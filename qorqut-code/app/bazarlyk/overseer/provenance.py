"""Preserve original event identity and exclude known Overseer-origin material.

Only trusted source metadata is accepted here; matching text is not proof that
one record is a copy of another.  The host is responsible for obtaining the
metadata from the canonical record, rather than a model's assertion.
"""
from __future__ import annotations

import hashlib
import json


def content_hash(value):
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def native_event_id(provider, conversation_id, message_id):
    """Stable identity for an explicitly linked native event, independent of transport."""
    return content_hash(["native-event", provider, conversation_id, message_id])


def provenance(*, producer_id=None, original_refs=(), overseer_origin=False, original_event_id=None):
    return {"producer_id": producer_id, "original_refs": list(original_refs),
            "overseer_origin": bool(overseer_origin),
            "independence": "known_origin" if original_event_id else "unknown"}


def source_ref(*, provider, kind, record_id, revision, content, episode_id, actor_id, actor_kind,
               occurred_at, locator, scope, namespace, producer_id=None, original_refs=(), original_event_id=None,
               overseer_origin=False, availability="available", material_ref=None, before_ref=None, after_ref=None):
    """Create a normalized reference without guessing an original or author."""
    value = {"provider": provider, "kind": kind, "record_id": record_id, "revision": revision,
             "content_hash": content_hash(content), "origin_event_id": original_event_id,
             "episode_id": episode_id, "actor_id": actor_id, "actor_kind": actor_kind,
             "occurred_at": occurred_at, "locator": locator, "scope": scope, "namespace": namespace,
             "provenance": provenance(producer_id=producer_id, original_refs=original_refs,
                                      overseer_origin=overseer_origin, original_event_id=original_event_id),
             "availability": availability}
    if material_ref is not None:
        value["material_ref"] = material_ref
    if before_ref is not None:
        value["before_ref"] = before_ref
    if after_ref is not None:
        value["after_ref"] = after_ref
    return value


def excluded(reference, *, overseer_ids, lookup, origin_check=None):
    """Follow only original-source references; terminate safely on cycles.

    `lookup` resolves an explicit trusted original reference to its normalized
    metadata, or None when it cannot be resolved. Unknown provenance is not
    promoted to an independently supported event.
    """
    pending, seen = [reference], set()
    while pending:
        ref = pending.pop()
        key = (ref.get("provider"), ref.get("kind"), ref.get("record_id"), str(ref.get("revision")))
        if key in seen:
            continue
        seen.add(key)
        meta = ref.get("provenance") or {}
        if origin_check is not None and origin_check(ref):
            return True
        if meta.get("overseer_origin") or meta.get("producer_id") in overseer_ids:
            return True
        originals = list(meta.get("original_refs", []))
        if ref.get("before_ref") is not None:
            originals.append(ref["before_ref"])
        for original in originals:
            if origin_check is not None and isinstance(original, dict) and origin_check(original):
                return True
            resolved = lookup(original)
            if resolved is not None:
                pending.append(resolved)
    return False
