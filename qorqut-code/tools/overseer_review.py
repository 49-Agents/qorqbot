#!/usr/bin/env python3
"""Lossless review pages and explicit coverage arguments for Overseer extraction.

    overseer_review.py assemble --out DIR RESPONSE.json [RESPONSE.json ...]
    overseer_review.py verify DIR
    overseer_review.py coverage DIR --run-id RUN [--reviewed REVIEWED.json] [--unresolved UNRESOLVED.json]
                                    [--next-cursor CURSOR] [--operation-prefix PREFIX]

Inputs are the exact JSON responses of the existing ``overseer_episode_get`` tool (or a full-source response with
``source_ref`` and ``content``), saved unedited as local files, in fetch order, each page inside the envelope
``{"request_cursor": <cursor sent, or null>, "response": <response>}``. Only a chain proven by those cursors, from a first
request without one to an explicit final ``next_cursor: null``, counts as fetch-complete; bare pages still assemble and verify
individual sources but leave their episode ``unverified``. The helper reads local files only: it holds no credentials and
calls no service.

``assemble`` groups the content fragments by exact source identity, requires contiguous offsets from zero, one coherent
reference per identity, ``json`` encoding, valid JSON and a SHA-256 equal to the reference's ``content_hash``, and writes the
complete text of every fully assembled source to numbered page files plus one ``manifest.json``. Pages only divide the text;
nothing is trimmed, filtered or summarized. Incomplete sources stay pending and inconsistent ones are rejected, both listed
in the manifest, never filled in. ``coverage`` turns an explicitly supplied list of exact reviewed references into
``overseer.coverage.record`` arguments. Nothing is ever inferred from a fetch: the list is the caller's attestation that
every rendered page of those sources was read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

FORMAT = "overseer-review-manifest"
VERSION = 1
MAX_REFS = 200  # the coverage operation's per-list limit
PAGE_CHARS = 40000
LINE_CHARS = 1000
MIN_PART = 200
IDENTITY_FIELDS = ("provider", "kind", "namespace", "record_id", "revision", "content_hash")
HASH = re.compile(r"[0-9a-f]{64}\Z")
OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
RECORD_KEYS = {"source_ref", "content_fragment", "encoding", "offset"}
PART_HEAD = re.compile(r"=== (S\d{4,}) part (\d+)/(\d+) chars (\d+)-(\d+) of (\d+) ===\Z")
PART_END = re.compile(r"=== end (S\d{4,}) part (\d+)/(\d+) sha256 ([0-9a-f]{64}) ===\Z")


class ReviewError(Exception):
    """A request the helper refuses outright; per-source problems go in the manifest instead."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(text, errors="strict"):
    return hashlib.sha256(text.encode("utf-8", errors)).hexdigest()


def input_digest(value):
    return digest(canonical(value), "surrogatepass")


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate JSON key: " + key)
        value[key] = item
    return value


def _constant(name):
    raise ValueError("non-finite JSON number: " + name)


def parse_documents(text, label="input"):
    """Every JSON document in a text: one, several concatenated or one per line."""
    decoder = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)
    documents, position = [], 0
    text = text.lstrip("﻿")
    while True:
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        if position >= len(text):
            break
        try:
            value, position = decoder.raw_decode(text, position)
        except ValueError as error:
            raise ReviewError(f"{label}: invalid JSON: {error}") from error
        documents.append(value)
    if not documents:
        raise ReviewError(f"{label}: no JSON document")
    return documents


def source_identity(ref, *, strict_namespace=False):
    """The exact-revision key used by the host, validated like the coverage operation validates it."""
    if not isinstance(ref, dict):
        raise ReviewError("a source reference must be an object")
    for field in ("provider", "kind", "record_id", "revision", "content_hash"):
        if not isinstance(ref.get(field), str) or not ref[field].strip():
            raise ReviewError(f"source_ref.{field} must be a nonempty string")
    if not HASH.match(ref["content_hash"]):
        raise ReviewError("source_ref.content_hash must be a SHA-256 hex digest")
    namespace = ref.get("namespace")
    if not isinstance(namespace, dict) or not {"kind", "business_id"} <= namespace.keys():
        raise ReviewError("source_ref.namespace must contain kind and business_id")
    kind, business = namespace["kind"], namespace["business_id"]
    if kind not in ("global", "business") or (kind == "global") != (business is None):
        raise ReviewError("source_ref.namespace is invalid")
    if kind == "business":
        if not isinstance(business, str) or not business.strip():
            raise ReviewError("source_ref.namespace.business_id must be a nonempty string")
        if strict_namespace:
            from uuid import UUID
            try:
                if str(UUID(business)) != business:
                    raise ValueError
            except ValueError as error:
                raise ReviewError("source_ref.namespace.business_id must be a canonical UUID") from error
    return canonical([ref["provider"], ref["kind"], {"kind": kind, "business_id": business}, ref["record_id"],
                      ref["revision"], ref["content_hash"]])


def identity_ref(ref):
    """The six identity fields only, in the shape the coverage operation keys on."""
    key = json.loads(source_identity(ref))
    return dict(zip(IDENTITY_FIELDS, key))


# ---- inputs ---------------------------------------------------------------------------------------------------

class Item:
    """One saved tool response; ``cursor`` is the request cursor when the saver recorded it (None = first page)."""

    def __init__(self, label, response, *, has_cursor=False, cursor=None):
        self.label, self.response, self.has_cursor, self.cursor = label, response, has_cursor, cursor


def _items(value, label):
    if isinstance(value, list):
        blocks = bool(value) and all(isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                                     for b in value)
        if blocks:
            return [item for number, block in enumerate(value)
                    for doc in parse_documents(block["text"], f"{label}[{number}]") for item in _items(doc, label)]
        return [item for number, entry in enumerate(value) for item in _items(entry, f"{label}[{number}]")]
    if not isinstance(value, dict):
        raise ReviewError(f"{label}: expected a tool response object")
    if "records" in value or {"source_ref", "content"} <= value.keys():
        return [Item(label, value)]
    if set(value) == {"request_cursor", "response"}:
        inner = _items(value["response"], label)
        if len(inner) != 1 or value["request_cursor"] is not None and not isinstance(value["request_cursor"], str):
            raise ReviewError(f"{label}: a request_cursor envelope wraps exactly one response")
        inner[0].has_cursor, inner[0].cursor = True, value["request_cursor"]
        return inner
    for key in ("result", "structuredContent", "content"):
        if key in value and len(value) <= 2:
            return _items(value[key], label)
    raise ReviewError(f"{label}: not an overseer_episode_get page or a full-source response")


def load_items(paths, stdin=None):
    items = []
    for path in paths:
        text = (stdin if stdin is not None else sys.stdin).read() if path == "-" else _read(path)
        label = "stdin" if path == "-" else str(path)
        for number, document in enumerate(parse_documents(text, label)):
            items.extend(_items(document, label if number == 0 else f"{label}#{number + 1}"))
    return items


# ---- assembly -------------------------------------------------------------------------------------------------

def _page(item, index, errors):
    """Validate one episode page; returns it with checked records, or None after recording why not."""
    response, where = item.response, {"input": item.label}

    def bad(message, code="malformed_response"):
        errors.append({"code": code, "message": message, **where})

    episode = response.get("id")
    frontier = response.get("frontier")
    if not isinstance(episode, str) or not episode:
        bad("episode page lacks an id")
        return None
    where["episode"] = episode
    if (not isinstance(frontier, dict) or not isinstance(frontier.get("snapshot_id"), str)
            or not isinstance(frontier.get("captured_at"), str) or not isinstance(frontier.get("providers"), list)):
        bad("episode page lacks a frontier with snapshot_id, captured_at and providers")
        return None
    cursor = response.get("next_cursor")
    if "next_cursor" not in response or cursor is not None and (not isinstance(cursor, str) or not cursor):
        bad("next_cursor must be present, either null (final page) or a nonempty string; a missing one is not final")
        return None
    if not isinstance(response.get("records"), list) or not isinstance(response.get("gaps"), list) \
            or not all(isinstance(gap, dict) for gap in response["gaps"]):
        bad("episode page lacks a records list or a gaps list of objects")
        return None
    if "cursor_reset" in response and type(response["cursor_reset"]) is not bool:
        bad("cursor_reset must be a boolean")
        return None
    records = []
    for number, record in enumerate(response["records"]):
        if not isinstance(record, dict) or set(record) != RECORD_KEYS:
            bad(f"record {number} must contain exactly {sorted(RECORD_KEYS)}")
            continue
        if record["encoding"] != "json":
            bad(f"record {number} has unsupported encoding {record['encoding']!r}", "unsupported_encoding")
            continue
        offset, text = record["offset"], record["content_fragment"]
        if type(offset) is not int or offset < 0 or not isinstance(text, str) or not text:
            bad(f"record {number} needs a nonnegative integer offset and a nonempty fragment")
            continue
        try:
            key = source_identity(record["source_ref"])
            if record["source_ref"].get("material_ref") is None:
                raise ReviewError("source_ref.material_ref is required")
        except ReviewError as error:
            bad(f"record {number}: {error}")
            continue
        records.append((key, record["source_ref"], offset, text))
    if "source_refs" in response and response["source_refs"] != [r.get("source_ref") if isinstance(r, dict) else None
                                                                 for r in response["records"]]:
        bad("source_refs disagree with the records")
    return {"index": index, "item": item, "id": episode, "cursor": cursor, "reset": response.get("cursor_reset") is True,
            "frontier": frontier, "records": records, "gaps": response["gaps"],
            "related": response.get("related_episodes") or [], "digest": input_digest(response)}


def assemble(items, *, page_chars=PAGE_CHARS, line_chars=LINE_CHARS):
    """Returns (manifest, page_texts). Per-source problems are listed in the manifest, never repaired."""
    if type(page_chars) is not int or type(line_chars) is not int or page_chars < 1 or line_chars < 1:
        raise ReviewError("page_chars and line_chars must be positive integers")
    errors, inputs, episodes_pages, sources, whole = [], [], {}, {}, []
    frontier = None
    for index, item in enumerate(items):
        response = item.response
        entry = {"index": index, "input": item.label, "sha256": input_digest(response)}
        inputs.append(entry)
        if "records" not in response:  # full-source response
            whole.append((index, item, entry))
            continue
        page = _page(item, index, errors)
        if page is None:
            if isinstance(response.get("id"), str):
                episodes_pages.setdefault(response["id"], []).append(None)
            continue
        entry["episode"] = page["id"]
        if frontier is None:
            frontier = {key: page["frontier"][key] for key in ("snapshot_id", "captured_at", "providers")}
            frontier_gaps = page["frontier"].get("gaps") or []
        elif (page["frontier"]["snapshot_id"], page["frontier"]["captured_at"]) != (frontier["snapshot_id"], frontier["captured_at"]):
            raise ReviewError(f"{item.label}: responses span more than one frontier snapshot; assemble each frontier separately")
        pages = episodes_pages.setdefault(page["id"], [])
        if any(other and other["digest"] == page["digest"] for other in pages):
            entry["duplicate"] = True
            continue
        pages.append(page)

    def source(key, ref):
        record = sources.get(key)
        if record is None:
            record = sources[key] = {"key": key, "ref": ref, "pieces": [], "chains": {}, "episodes": [], "problems": [],
                                     "pending": []}
        return record

    def fragment(chain, finished, key, ref, offset, text, label, episode):
        record = source(key, ref)
        if canonical(record["ref"]) != canonical(ref):
            record["problems"].append(("incoherent_ref", "fragments of one identity carry different references "
                                       "(material_ref, provenance or locator disagree)", label))
            return
        record["pieces"].append((chain, offset, text, label))
        record["chains"][chain] = finished
        if episode is not None and episode not in record["episodes"]:
            record["episodes"].append(episode)

    episodes, all_gaps = [], []
    for episode, pages in episodes_pages.items():
        broken = any(page is None for page in pages)
        chain, superseded = [], []
        for page in (p for p in pages if p):
            if page["reset"] and chain:
                superseded.extend(chain)
                chain = []
            if chain and chain[-1]["cursor"] is None:
                errors.append({"code": "page_after_final", "episode": episode, "input": page["item"].label,
                               "message": "a different page follows the page with a null next_cursor"})
            chain.append(page)
        linked = bool(chain) and all(p["item"].has_cursor for p in chain)
        for before, after in zip(chain, chain[1:]):
            if after["item"].has_cursor and before["item"].has_cursor and after["item"].cursor != before["cursor"]:
                linked = False
                errors.append({"code": "cursor_chain_broken", "episode": episode, "input": after["item"].label,
                               "message": "the request cursor is not the previous page's next_cursor; a page was skipped"})
        if linked and chain[0]["item"].cursor is not None and not chain[0]["reset"]:
            linked = False
            errors.append({"code": "cursor_chain_broken", "episode": episode, "input": chain[0]["item"].label,
                           "message": "the first supplied page was not requested without a cursor; earlier pages are missing"})
        finished = bool(chain) and chain[-1]["cursor"] is None and not broken
        # Completeness needs a proven chain: every page saved with its request_cursor, the first requested without one (or
        # a valid cursor_reset), each request cursor equal to the previous next_cursor, and an explicit null at the end.
        if broken or any(error.get("episode") == episode for error in errors):
            state = "broken"
        elif not chain or chain[-1]["cursor"] is not None:
            state = "unfinished"
        else:
            state = "complete" if linked else "unverified"
        related = [link for page in chain for link in page["related"] if isinstance(link, dict)]
        episodes.append({"id": episode, "pages": [p["index"] for p in chain], "superseded_pages": [p["index"] for p in superseded],
                         "finished": finished, "chain": state, "next_cursor": chain[-1]["cursor"] if chain else None,
                         "cursor_linkage": "verified" if linked else "unverified",
                         "related_captured": sum(1 for link in related if link.get("captured") is True),
                         "related_uncaptured": sum(1 for link in related if link.get("captured") is not True)})
        for page in chain:
            all_gaps.extend(page["gaps"])
            for key, ref, offset, text in page["records"]:
                fragment(episode, finished, key, ref, offset, text, page["item"].label, episode)

    for number, (index, item, entry) in enumerate(whole):
        response = item.response
        try:
            key = source_identity(response["source_ref"])
            if response["source_ref"].get("material_ref") is None:
                raise ReviewError("source_ref.material_ref is required")
            if response.get("availability", "available") != "available":
                source(key, response["source_ref"])["pending"].append("source_unavailable")
                continue
            fragment("source:%d" % index, True, key, response["source_ref"], 0, canonical(response["content"]), item.label, None)
        except (ReviewError, KeyError, ValueError) as error:
            errors.append({"code": "malformed_response", "input": item.label, "message": f"full-source response: {error}"})

    ordered = list(sources.values())  # first-appearance order
    texts, out_sources = {}, []
    for number, record in enumerate(ordered, 1):
        sid = "S%04d" % number
        status, text = _assemble(record, errors, sid)
        if status == "assembled":
            texts[sid] = text
        out_sources.append({"id": sid, "key": record["key"], "ref": record["ref"], "status": status,
                            "length": len(text) if status == "assembled" else None,
                            "content_hash": record["ref"]["content_hash"], "episodes": record["episodes"],
                            "reasons": record["pending"] + [problem[1] for problem in record["problems"]
                                                            if status == "rejected"]})
    batch = digest(canonical([entry["sha256"] for entry in inputs]))
    pages, layout = _render(out_sources, texts, batch, page_chars, line_chars)
    for entry in out_sources:
        entry["pages"] = sorted({p["number"] for p in layout for part in p["parts"] if part["source"] == entry["id"]})
    gaps = sorted({canonical(gap): gap for gap in all_gaps}.values(), key=canonical)
    inventory = sorted({canonical(gap): gap for gap in (frontier_gaps if frontier else []) if isinstance(gap, dict)}.values(),
                       key=canonical)
    counts = {status: sum(1 for s in out_sources if s["status"] == status) for status in ("assembled", "pending", "rejected")}
    manifest = {"format": FORMAT, "version": VERSION, "batch_id": batch, "page_chars": page_chars, "line_chars": line_chars,
                "frontier": frontier, "inventory_gaps": inventory, "inputs": inputs, "episodes": episodes,
                "pages": layout, "sources": out_sources, "gaps": gaps, "errors": errors,
                "summary": {"fetch_complete": all(e["chain"] == "complete" for e in episodes) and not errors
                            and not counts["pending"] and not counts["rejected"], "sources": len(out_sources), **counts,
                            "pages": len(pages), "chains": {state: sum(1 for e in episodes if e["chain"] == state)
                                                            for state in ("complete", "unverified", "unfinished", "broken")}}}
    return manifest, pages


def _assemble(record, errors, sid):
    """Overlay the pieces of one identity; returns (status, text). Rejections are appended to ``errors``."""
    problems = record["problems"]
    if record["pending"] and not record["pieces"] and not problems:
        return "pending", ""
    by_chain = {}
    for chain, offset, text, label in record["pieces"]:
        by_chain.setdefault(chain, []).append((offset, text, label))
    pieces = []
    for chain, found in by_chain.items():
        found.sort(key=lambda piece: (piece[0], piece[1]))
        last = None
        for offset, text, label in found:
            if last is not None and offset == last[0]:
                if text != last[1]:
                    problems.append(("conflicting_fragment", f"two different fragments at offset {offset}", label))
                continue
            if last is not None and offset < last[0] + len(last[1]):
                problems.append(("overlapping_fragments", f"fragments overlap at offset {offset}", label))
                continue
            pieces.append((offset, text))
            last = (offset, text)
    pieces.sort()
    buf, end, missing = "", 0, None
    if not problems:
        for offset, text in pieces:
            if offset > end:
                missing = (end, offset)
                break
            overlap = min(end, offset + len(text))
            if text[:overlap - offset] != buf[offset:overlap]:
                problems.append(("conflicting_fragment", f"fragments from different pages disagree near offset {offset}", ""))
                break
            if offset + len(text) > end:
                buf += text[end - offset:]
                end = offset + len(text)
    if missing and not problems:
        problems.append(("missing_fragment", f"no fragment covers characters {missing[0]}-{missing[1]}", ""))
    if not problems:
        try:
            matches = digest(buf) == record["ref"]["content_hash"]
        except UnicodeEncodeError:
            matches = False
            problems.append(("not_utf8_encodable", "the reassembled text cannot be encoded as UTF-8", ""))
        if matches:
            try:
                json.loads(buf, object_pairs_hook=_pairs, parse_constant=_constant)
            except ValueError as error:
                problems.append(("invalid_json", f"the reassembled text is not valid JSON: {error}", ""))
        elif not problems:
            if all(not finished for finished in record["chains"].values()) and record["chains"]:
                record["pending"].append(f"awaiting continuation: {end} characters received so far")
                return "pending", ""
            problems.append(("content_hash_mismatch", f"the {end} reassembled characters do not match content_hash "
                             "(trailing fragments missing or content altered)", ""))
    if problems:
        for code, message, label in problems:
            errors.append({"code": code, "message": message, "source": sid, **({"input": label} if label else {})})
        return "rejected", ""
    if record["pending"]:
        return "pending", ""
    return "assembled", buf


# ---- pages ----------------------------------------------------------------------------------------------------

def page_name(number):
    return "page-%04d.txt" % number


def _wrap(text, width):
    """Display lines of at most ``width`` characters that join back to ``text`` exactly, preferring to end at a space."""
    lines, position = [], 0
    while position < len(text):
        end = min(position + width, len(text))
        if end < len(text):
            cut = text.rfind(" ", position + width * 3 // 5, end)
            end = cut + 1 if cut != -1 else end
        lines.append(text[position:end])
        position = end
    return lines


def _render(sources, texts, batch, page_chars, line_chars):
    layout = [[]]
    used = 0
    for entry in sources:
        text = texts.get(entry["id"])
        if text is None:
            continue
        position = 0
        if used and len(text) <= page_chars and len(text) > page_chars - used:
            layout.append([])
            used = 0
        while position < len(text):
            room = page_chars - used
            if used and room < min(MIN_PART, len(text) - position):
                layout.append([])
                used, room = 0, page_chars
            take = min(room, len(text) - position)
            layout[-1].append([entry["id"], position, position + take])
            used += take
            position += take
    if layout == [[]]:
        layout = []
    totals = {}
    for page in layout:
        for sid, *_ in page:
            totals[sid] = totals.get(sid, 0) + 1
    counter, pages, described = {}, [], []
    by_id = {entry["id"]: entry for entry in sources}
    for number, parts in enumerate(layout, 1):
        lines = [f"OVERSEER REVIEW PAGE {number}/{len(layout)}  batch {batch}",
                 'Lines starting with "| " are the exact source text (canonical JSON). Join the lines of one part with no '
                 "separator: wrapping is for display only. Nothing is cut, filtered or summarized. Attest a source only "
                 "after reading all of its parts."]
        entries = []
        for sid, start, end in parts:
            counter[sid] = counter.get(sid, 0) + 1
            text, ref = texts[sid], by_id[sid]["ref"]
            lines += [f"=== {sid} part {counter[sid]}/{totals[sid]} chars {start}-{end} of {len(text)} ===",
                      f"provider={ref['provider']} kind={ref['kind']} record_id={ref['record_id']} revision={ref['revision']} "
                      f"namespace={canonical(ref['namespace'])} content_hash={ref['content_hash']}"]
            lines += ["| " + line for line in _wrap(text[start:end], line_chars)]
            lines.append(f"=== end {sid} part {counter[sid]}/{totals[sid]} sha256 {digest(text[start:end])} ===")
            entries.append({"source": sid, "part": counter[sid], "parts": totals[sid], "start": start, "end": end})
        lines.append(f"=== END OF PAGE {number}/{len(layout)} " + (f"next: {page_name(number + 1)} ===" if number < len(layout)
                                                               else "last page ===") )
        text = "\n".join(lines) + "\n"
        pages.append(text)
        described.append({"number": number, "file": page_name(number), "sha256": digest(text), "parts": entries})
    return pages, described


def parse_page(text):
    """Parts of one page file: (source id, part, parts, start, end, total, content, declared slice sha256)."""
    lines = text.split("\n")
    parts, current = [], None
    for line in lines:
        head = PART_HEAD.match(line)
        if head:
            current = [head.group(1), *map(int, head.groups()[1:]), []]
            continue
        end = PART_END.match(line)
        if end and current and end.group(1) == current[0]:
            parts.append((current[0], current[1], current[2], current[3], current[4], current[5], "".join(current[6]),
                          end.group(4)))
            current = None
        elif current is not None and line.startswith("| "):
            current[6].append(line[2:])
    return parts


def verify_pages(manifest, pages):
    """Problems (empty when ok) between a manifest and its page texts; also proves each source is reconstructable."""
    problems = []
    if len(pages) != len(manifest["pages"]):
        problems.append(f"expected {len(manifest['pages'])} pages, found {len(pages)}")
    found = {}
    for described, text in zip(manifest["pages"], pages):
        if digest(text) != described["sha256"]:
            problems.append(f"{described['file']} differs from the manifest")
        if not text.endswith(f"=== END OF PAGE {described['number']}/{len(manifest['pages'])} " + (
                f"next: {page_name(described['number'] + 1)} ===\n" if described["number"] < len(manifest["pages"])
                else "last page ===\n")):
            problems.append(f"{described['file']} lacks its end marker")
        for sid, part, parts, start, end, total, content, declared in parse_page(text):
            if digest(content) != declared or len(content) != end - start:
                problems.append(f"{described['file']}: part {part} of {sid} is damaged")
            found.setdefault(sid, []).append((start, content, total))
    for entry in manifest["sources"]:
        pieces = sorted(found.pop(entry["id"], []))
        if entry["status"] != "assembled":
            if pieces:
                problems.append(f"{entry['id']} is {entry['status']} but has page content")
            continue
        text = "".join(piece[1] for piece in pieces)
        contiguous = all(a[0] + len(a[1]) == b[0] for a, b in zip(pieces, pieces[1:])) and pieces and pieces[0][0] == 0
        if not contiguous or len(text) != entry["length"] or digest(text) != entry["content_hash"]:
            problems.append(f"{entry['id']} cannot be reconstructed exactly from the pages")
    problems += [f"{sid} appears on pages but not in the manifest" for sid in found]
    return problems


def reconstruct(manifest, pages):
    """Source id -> exact assembled text, read back from the page files."""
    parts = {}
    for text in pages:
        for sid, _, _, start, _, _, content, _ in parse_page(text):
            parts.setdefault(sid, []).append((start, content))
    return {sid: "".join(content for _, content in sorted(found)) for sid, found in parts.items()}


def write_batch(manifest, pages, out, *, replace=False):
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        if not replace:
            raise ReviewError(f"{out} is not empty; use --replace to rewrite a previous batch")
        for old in list(out.glob("page-*.txt")) + [out / "manifest.json"]:
            old.unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    for described, text in zip(manifest["pages"], pages):
        (out / described["file"]).write_text(text, encoding="utf-8", newline="\n")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                                       encoding="utf-8", newline="\n")


def load_batch(directory):
    directory = Path(directory)
    try:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        pages = [(directory / page["file"]).read_text(encoding="utf-8") for page in manifest["pages"]]
    except (OSError, ValueError, KeyError) as error:
        raise ReviewError(f"{directory}: cannot read the batch: {error}") from error
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise ReviewError(f"{directory}: not an {FORMAT} version {VERSION}")
    return manifest, pages


# ---- coverage -------------------------------------------------------------------------------------------------

def validate_coverage_record(record):
    """The coverage operation's own structural rules, applied before anything is emitted."""
    if set(record) != {"schema_version", "run_id", "frontier", "analyzed_sources", "pending_sources", "gaps", "complete",
                       "next_cursor"} or record["schema_version"] != 1 or type(record["schema_version"]) is not int:
        raise ReviewError("coverage record has the wrong fields")
    frontier = record["frontier"]
    if (not isinstance(record["run_id"], str) or not record["run_id"].strip() or not isinstance(frontier, dict)
            or not {"snapshot_id", "captured_at", "providers"} <= frontier.keys()
            or not isinstance(frontier["captured_at"], str) or not isinstance(frontier["providers"], list)
            or not all(isinstance(p, dict) and {"provider", "anchor"} <= p.keys() and isinstance(p["provider"], str)
                       and p["provider"].strip() for p in frontier["providers"])):
        raise ReviewError("coverage record needs a run_id and a frontier with snapshot_id, captured_at and providers")
    from uuid import UUID
    try:
        if str(UUID(frontier["snapshot_id"])) != frontier["snapshot_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise ReviewError("frontier.snapshot_id must be a canonical UUID") from error
    keys = {}
    for field in ("analyzed_sources", "pending_sources"):
        refs = record[field]
        if not isinstance(refs, list) or len(refs) > MAX_REFS:
            raise ReviewError(f"{field} must be a list of at most {MAX_REFS} references")
        keys[field] = [source_identity(ref, strict_namespace=True) for ref in refs]
        if len(set(keys[field])) != len(keys[field]):
            raise ReviewError(f"{field} repeats an exact source reference")
    if set(keys["analyzed_sources"]) & set(keys["pending_sources"]):
        raise ReviewError("pending_sources overlaps analyzed_sources")
    if not isinstance(record["gaps"], list) or type(record["complete"]) is not bool:
        raise ReviewError("gaps must be a list and complete a boolean")
    if record["next_cursor"] is not None and (not isinstance(record["next_cursor"], str) or not record["next_cursor"].strip()):
        raise ReviewError("next_cursor must be null or a nonempty string")
    if record["complete"] and (record["pending_sources"] or record["gaps"] or record["next_cursor"] is not None):
        raise ReviewError("complete requires no pending sources, gaps or continuation")


def _requested(manifest, refs, what):
    """Resolve caller-supplied references to this batch's assembled-or-not sources; any mismatch refuses everything."""
    by_key = {source["key"]: source for source in manifest["sources"]}
    found, problems = {}, []
    for number, ref in enumerate(refs):
        try:
            key = source_identity(ref)
        except ReviewError as error:
            problems.append(f"{what}[{number}]: {error}")
            continue
        source = by_key.get(key)
        if source is None:
            problems.append(f"{what}[{number}]: {ref.get('record_id')} revision {ref.get('revision')} is not in this batch "
                            "(stale or mismatched reference)")
        elif any(field not in IDENTITY_FIELDS and source["ref"].get(field) != value for field, value in ref.items()):
            problems.append(f"{what}[{number}]: {source['id']} differs from the batch's reference (stale or mismatched)")
        else:
            found[key] = source
    if problems:
        raise ReviewError("; ".join(problems))
    return list(found.values())


def build_coverage(manifest, run_id, *, reviewed=(), unresolved=(), next_cursor=None, operation_prefix="overseer-coverage"):
    """``overseer.coverage.record`` requests for this batch; ``reviewed`` is the caller's explicit attestation."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ReviewError("an admitted extraction run_id is required")
    if manifest.get("frontier") is None:
        raise ReviewError("the batch has no frontier: coverage needs at least one episode page")
    if not OPERATION_ID.match(operation_prefix + ":0") or operation_prefix.lower().startswith("run:"):
        raise ReviewError("operation prefix must be a valid operation ID start and not use the reserved run: namespace")
    chosen = _requested(manifest, list(reviewed), "reviewed_sources")
    unfit = [s["id"] + " is " + s["status"] for s in chosen if s["status"] != "assembled"]
    if unfit:
        raise ReviewError("sources without complete verified content cannot be marked reviewed: " + ", ".join(unfit))
    present, reasons, absent = {s["key"]: s for s in manifest["sources"]}, [], {}
    for number, entry in enumerate(unresolved):
        if not isinstance(entry, dict) or set(entry) != {"source_ref", "reason"} or not isinstance(entry["reason"], str) \
                or not entry["reason"].strip() or len(entry["reason"]) > 2000:
            raise ReviewError(f"unresolved[{number}] needs a source_ref and a reason of 1-2000 characters")
        ref = entry["source_ref"]
        try:
            key = source_identity(ref, strict_namespace=True)
        except ReviewError as error:
            raise ReviewError(f"unresolved[{number}]: {error}") from error
        if key in present:
            _requested(manifest, [ref], f"unresolved[{number}]")  # a source in the batch must carry coherent metadata
        elif absent.setdefault(key, ref) != ref:
            raise ReviewError(f"unresolved[{number}]: the same source is declared twice with different reference metadata")
        reasons.append((key, identity_ref(ref), entry["reason"]))
    analyzed = {s["key"]: s for s in chosen}
    clash = [present[key]["id"] for key, _, _ in reasons if key in analyzed]
    if clash:
        raise ReviewError("sources cannot be both reviewed and unresolved: " + ", ".join(clash))
    gaps = list(manifest["gaps"]) + [{"code": "unresolved_source", "source": identity, "reason": reason}
                                      for _, identity, reason in reasons]
    gaps = list({canonical(gap): gap for gap in gaps}.values())
    gaps.sort(key=canonical)
    ordered = sorted(manifest["sources"], key=lambda s: s["key"])
    done = [s["ref"] for s in ordered if s["key"] in analyzed]
    waiting = {s["key"]: s["ref"] for s in ordered if s["key"] not in analyzed}
    waiting.update(absent)  # originally saved references the batch could not deliver: declared unread, never analyzed
    pending = [waiting[key] for key in sorted(waiting)]
    errors = manifest["errors"]
    whole = (not errors and not pending and not gaps and next_cursor is None and bool(done)
             and all(e["chain"] == "complete" for e in manifest["episodes"]))
    count = max(-(-len(done) // MAX_REFS), -(-len(pending) // MAX_REFS), 1)
    requests = []
    for index in range(count):
        record = {"schema_version": 1, "run_id": run_id, "frontier": manifest["frontier"],
                  "analyzed_sources": done[index * MAX_REFS:(index + 1) * MAX_REFS],
                  "pending_sources": pending[index * MAX_REFS:(index + 1) * MAX_REFS],
                  "gaps": gaps if index == 0 else [], "complete": whole and index == count - 1,
                  "next_cursor": next_cursor if index == count - 1 else None}
        validate_coverage_record(record)
        requests.append({"operation": "overseer.coverage.record",
                         "arguments": {"record": record, "operation_id": f"{operation_prefix}:{digest(canonical(record))[:40]}"}})
    return {"requests": requests, "summary": {
        "batch_id": manifest["batch_id"], "requests": count, "analyzed_sources": len(done), "pending_sources": len(pending),
        "gaps": len(gaps), "unresolved_not_in_batch": len(absent), "chains": manifest["summary"]["chains"],
        "fetch_complete": manifest["summary"]["fetch_complete"], "batch_complete": whole,
        "errors": len(errors), "note": "analyzed_sources is only what the reviewed list attests; this does not finish the "
                                         "workflow, and complete describes this batch only"}}


# ---- command line ---------------------------------------------------------------------------------------------

def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ReviewError(f"{path}: cannot read: {error}") from error


def _json(path):
    return parse_documents(_read(path), str(path))[0]


def _list(value, key):
    value = value.get(key) if isinstance(value, dict) else value
    if not isinstance(value, list):
        raise ReviewError(f"expected a JSON list (or an object with {key})")
    return value


def main(argv=None, stdin=None, stdout=None):
    out = stdout or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("assemble")
    build.add_argument("--out", required=True)
    build.add_argument("--page-chars", type=int, default=PAGE_CHARS)
    build.add_argument("--line-chars", type=int, default=LINE_CHARS)
    build.add_argument("--replace", action="store_true")
    build.add_argument("responses", nargs="+")
    check = commands.add_parser("verify")
    check.add_argument("batch")
    cover = commands.add_parser("coverage")
    cover.add_argument("batch")
    cover.add_argument("--run-id", required=True)
    cover.add_argument("--reviewed")
    cover.add_argument("--unresolved")
    cover.add_argument("--next-cursor")
    cover.add_argument("--operation-prefix", default="overseer-coverage")
    args = parser.parse_args(argv)
    try:
        if args.command == "assemble":
            manifest, pages = assemble(load_items(args.responses, stdin), page_chars=args.page_chars, line_chars=args.line_chars)
            problems = verify_pages(manifest, pages)
            if problems:
                raise ReviewError("internal rendering check failed: " + "; ".join(problems))
            write_batch(manifest, pages, args.out, replace=args.replace)
            print(json.dumps({"out": args.out, "read_every_page": [p["file"] for p in manifest["pages"]],
                              "summary": manifest["summary"], "errors": manifest["errors"],
                              "pending": [{"source": s["id"], "reasons": s["reasons"]} for s in manifest["sources"]
                                          if s["status"] == "pending"],
                              "episodes_not_proven_complete": [{"id": e["id"], "chain": e["chain"]} for e in manifest["episodes"]
                                                             if e["chain"] != "complete"]},
                             indent=2, ensure_ascii=False), file=out)
            return 1 if manifest["errors"] else 0
        manifest, pages = load_batch(args.batch)
        problems = verify_pages(manifest, pages)
        if args.command == "verify":
            print(json.dumps({"ok": not problems, "problems": problems}, indent=2), file=out)
            return 1 if problems else 0
        if problems:
            raise ReviewError("the batch on disk no longer matches its manifest: " + "; ".join(problems))
        reviewed = _list(_json(args.reviewed), "reviewed_sources") if args.reviewed else []
        unresolved = _list(_json(args.unresolved), "unresolved") if args.unresolved else []
        print(json.dumps(build_coverage(manifest, args.run_id, reviewed=reviewed, unresolved=unresolved,
                                        next_cursor=args.next_cursor, operation_prefix=args.operation_prefix),
                         indent=2, ensure_ascii=False), file=out)
        return 0
    except ReviewError as error:
        print(f"overseer_review: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
