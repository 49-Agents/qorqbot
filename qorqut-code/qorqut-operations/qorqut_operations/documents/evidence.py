"""Exact source selection shared by filing preparation and application."""

import re

from qorqut_operations.contracts import DomainError
from .materials import MATERIAL_TYPES

SOURCE_SPANS = {"type": "array", "minItems": 1, "maxItems": 4000, "items": {
    "type": "object", "properties": {"start": {"type": "integer", "minimum": 0},
        "end": {"type": "integer", "minimum": 1}}, "required": ["start", "end"], "additionalProperties": False}}


def paragraphs(body):
    starts = list(dict.fromkeys([0, *[match.end() for match in re.finditer(r"\n[ \t]*\n", body)], len(body)]))
    return [body[start:end] for start, end in zip(starts, starts[1:]) if end > start]


def source_spans(chunks, ids):
    spans, offset = [], 0
    for index, chunk in enumerate(chunks):
        if index in ids:
            if spans and spans[-1]["end"] == offset: spans[-1]["end"] += len(chunk)
            else: spans.append({"start": offset, "end": offset + len(chunk)})
        offset += len(chunk)
    return spans


def extract(body, spans, *, conversation=False):
    if not spans or (conversation and len(spans) != 1):
        raise DomainError("validation_error", "A conversation must preserve one complete contiguous source passage")
    end, pieces = 0, []
    for span in spans:
        start, stop = span["start"], span["end"]
        if type(start) is not int or type(stop) is not int or not end <= start < stop <= len(body):
            raise DomainError("validation_error", "Source spans must be ordered, nonoverlapping and within the upload")
        pieces.append(body[start:stop])
        end = stop
    return "".join(pieces)
