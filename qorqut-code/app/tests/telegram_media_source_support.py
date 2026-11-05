"""A synthetic host for the Telegram media source checks: two businesses, the registered Ordubashy and a second Global agent.

Everything lives under a pytest ``tmp_path``; no operational data, credential, provider, model or network is used. Documents
are registered as rows plus files so every kind (including ones no operation creates for an agent) can be exercised against
the real canonical readers and document store."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from qorqut_operations.contracts import DomainError, Principal, new_id, utc_now

from bazarlyk.composition import locations
from qorqut_operations.documents.business_notes import KIND as BUSINESS_NOTE
from host_foundation_support import build_instance, create_registered_global, create_worker, register_sources

__all__ = ["BUSINESS_NOTE", "agent_context", "build_host", "register_document", "set_archived", "set_field"]


def build_host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("acme", "beta"))
    for slug in ("acme", "beta"):
        register_sources(instance, ids[slug], slug)
    ordubashy = create_registered_global(instance, "ordubashy")
    other = create_worker(instance, "colleague")
    instance.runtime.freeze_registry()
    return SimpleNamespace(instance=instance, runtime=instance.runtime, paths=instance.paths, database=instance.database,
                           acme=ids["acme"], beta=ids["beta"], ordubashy=ordubashy, other=other, tmp=tmp_path)


def agent_context(host, identity, business=None):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def register_document(host, *, kind, path, data=b"data", business_id=None, owner=None, metadata=None, archived=False,
                      availability="ready", write=True):
    """One registered document row and (unless ``write`` is false) its file at the location the store resolves for it."""
    identity = new_id()
    with host.database.transaction() as uow:
        business = None if business_id is None else uow.repository("businesses").get(business_id, required=True)
        try:
            target = locations.document_root(host.paths, business, path) / path
        except DomainError:
            # A location the composition refuses for managed documents still gets a row, so its refusal can be checked.
            target = None
        now = utc_now()
        uow.connection.execute(
            "INSERT INTO documents(id, business_id, namespace, owning_agent_id, kind, path, content_hash, metadata_json, "
            "source_refs_json, availability, archived, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, business_id, business_id or "global", owner, kind, path, hashlib.sha256(data).hexdigest(),
             json.dumps(metadata or {}), "[]", availability, int(archived), now, now))
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return SimpleNamespace(id=identity, business_id=business_id, kind=kind, path=path, file=target, data=data)


def set_field(host, document, column, value):
    with host.database.transaction() as uow:
        uow.connection.execute(f"UPDATE documents SET {column} = ?, revision = revision + 1 WHERE id = ?", (value, document.id))


def set_archived(host, document, archived=True):
    set_field(host, document, "archived", int(archived))
