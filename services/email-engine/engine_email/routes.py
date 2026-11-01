"""Trusted-admin managed Outreach routes: archive-only mailboxes and scoped client grants.

See docs/INTEGRATION-CONTRACT.md "Managed outreach routes". No credential appears in any result.
"""

import hashlib
import secrets

from pydantic import BaseModel, ConfigDict, Field

from .account import AccountAction, AccountMailService
from .fastmail import ProviderError
from .mailbox_settings import register
from .models import Mailbox
from .store import now

REF = r"^[A-Za-z0-9_-]{1,100}$"
REVOKED_REASON = "integration_route_revoked"


class RouteRejected(ValueError):
    """The request is valid JSON but not permitted for this client or sender."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class RouteConflict(ValueError):
    """Stale generation, reused request ID or an immutable binding change."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class RouteEnsure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    business_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    sender_ref: str = Field(pattern=REF)
    generation: int = Field(ge=1, le=2**53)
    active: bool = True
    mailbox_ref: str | None = Field(default=None, pattern=REF)


class RouteDeactivate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    business_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    generation: int = Field(ge=1, le=2**53)


def _key(client_id, business_id):
    return hashlib.sha256(f"{client_id}:{business_id}".encode()).hexdigest()[:32]


def route_id(client_id, business_id):
    return "route-" + _key(client_id, business_id)


def archive_ref(client_id, business_id):
    return "oa-" + _key(client_id, business_id)


def _row(db, client_id, business_id):
    row = db.execute("SELECT * FROM outreach_routes WHERE client_id=? AND business_id=?",
        (client_id, business_id)).fetchone()
    return dict(row) if row else None


def routes_for(store, client_id):
    with store.connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM outreach_routes WHERE client_id=? ORDER BY business_id", (client_id,))]


def route(store, client_id, business_id):
    with store.connect() as db:
        return _row(db, client_id, business_id)


def _save(store, current, changes):
    """Persist route changes, bumping its revision only when a fact changed."""
    values = {**current, **changes}
    changed = any(current.get(key) != values[key] for key in changes if key not in {"updated_at"})
    if changed:
        values["revision"] = current["revision"] + 1
        values["updated_at"] = now()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""UPDATE outreach_routes SET sender_ref=?,mailbox_ref=?,purpose=?,folder_name=?,folder_id=?,
            folder_attempts=?,active=?,generation=?,state=?,blocker=?,revision=?,updated_at=? WHERE route_id=?""",
            tuple(values[key] for key in ("sender_ref", "mailbox_ref", "purpose", "folder_name", "folder_id",
                "folder_attempts", "active", "generation", "state", "blocker", "revision", "updated_at", "route_id")))
    return values


def view(service, row, *, request_id=None, operation=None):
    sender = next((item for item in service.settings.shared_senders if item.ref == row["sender_ref"]), None)
    sender_available = bool(sender and sender.enabled and sender.permits_business(row["business_id"]))
    ready = row["state"] == "ready" and bool(row["active"]) and sender_available
    blocker = row["blocker"]
    if row["state"] == "ready" and row["active"] and not sender_available:
        blocker = "sender_unavailable"
    result = {"route_id": row["route_id"], "revision": row["revision"], "generation": row["generation"],
        "client_id": row["client_id"], "business_id": row["business_id"], "mailbox_ref": row["mailbox_ref"],
        "purpose": row["purpose"], "sender_ref": row["sender_ref"], "active": bool(row["active"]),
        "state": row["state"], "ready": ready, "blocker": blocker, "updated_at": row["updated_at"]}
    if request_id is not None:
        result["receipt"] = {"request_id": request_id, **({"folder_operation": operation} if operation else {})}
    return result


def _begin(service, request, digest):
    """Record the request and the desired route before any provider effect."""
    store = service.store
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        receipt = db.execute("SELECT client_id,digest FROM outreach_route_requests WHERE request_id=?",
            (request.request_id,)).fetchone()
        if receipt and (receipt[0] != request.client_id or receipt[1] != digest):
            raise RouteConflict("route_request_conflict", "Route request ID was already used for another change")
        current = _row(db, request.client_id, request.business_id)
        active = getattr(request, "active", False)
        sender_ref = getattr(request, "sender_ref", None)
        mailbox_ref = getattr(request, "mailbox_ref", None)
        if current and not receipt:
            if request.generation < current["generation"]:
                raise RouteConflict("stale_generation", "A newer route generation was already recorded")
            same = (bool(current["active"]) == active
                and (sender_ref is None or current["sender_ref"] in {None, sender_ref})
                and (mailbox_ref is None or current["mailbox_ref"] in {None, mailbox_ref}))
            if request.generation == current["generation"] and not same:
                raise RouteConflict("stale_generation", "This generation already recorded a different route")
        if current and sender_ref and current["sender_ref"] not in {None, sender_ref}:
            raise RouteConflict("route_sender_immutable", "A route's shared sender cannot be reassigned")
        if current and mailbox_ref and current["mailbox_ref"] not in {None, mailbox_ref}:
            raise RouteConflict("route_mailbox_immutable", "A route's mailbox cannot be reassigned")
        if not receipt:
            db.execute("INSERT INTO outreach_route_requests VALUES(?,?,?,?)",
                (request.request_id, request.client_id, digest, "{}"))
        if current is None:
            stamp = now()
            db.execute("""INSERT INTO outreach_routes(route_id,client_id,business_id,sender_ref,mailbox_ref,purpose,
                folder_name,folder_id,folder_attempts,active,generation,state,blocker,revision,created_at,updated_at)
                VALUES(?,?,?,?,?,?,NULL,NULL,0,?,?,?,NULL,1,?,?)""",
                (route_id(request.client_id, request.business_id), request.client_id, request.business_id,
                 sender_ref, mailbox_ref, None, int(active), request.generation,
                 "provisioning" if active else "disabled", stamp, stamp))
            return _row(db, request.client_id, request.business_id), True
        if receipt and request.generation < current["generation"]:
            # An exact replay of a superseded request reports the current route unchanged.
            return current, False
    changes = {"generation": request.generation, "active": int(active)}
    if sender_ref and current["sender_ref"] is None:
        changes["sender_ref"] = sender_ref
    if mailbox_ref and current["mailbox_ref"] is None:
        changes["mailbox_ref"] = mailbox_ref
    if active and current["state"] == "disabled":
        changes.update(state="provisioning", blocker=None)
    elif not active:
        changes.update(state="disabled", blocker=None)
    return _save(store, current, changes), True


def client_for(service, client_id):
    client = next((item for item in service.settings.integrations if item.client_id == client_id), None)
    if client is None:
        raise RouteRejected("unknown_client", "Integration client is not configured")
    return client


def ensure(service, request):
    client = client_for(service, request.client_id)
    if not client.managed_routes:
        raise RouteRejected("managed_routes_disabled", "Managed routes are not enabled for this client")
    if client.business_ids and request.business_id not in client.business_ids:
        raise RouteRejected("client_business_restricted", "The client's static business scope excludes this business")
    if not request.active:
        return deactivate(service, RouteDeactivate(request_id=request.request_id, client_id=request.client_id,
            business_id=request.business_id, generation=request.generation), digest_source=request)
    sender = next((item for item in service.settings.shared_senders if item.ref == request.sender_ref), None)
    if sender is None or not sender.permits_business(request.business_id):
        raise RouteRejected("sender_outside_business", "The shared sender does not grant this business")
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    with service.send_lock, service.sync_lock:
        row, _ = _begin(service, request, digest)
        if row["generation"] > request.generation or not row["active"]:
            return view(service, row, request_id=request.request_id)
        row, operation = _provision(service, client, row)
        return view(service, row, request_id=request.request_id, operation=operation)


def _provision(service, client, row):
    """Bind the explicit mailbox or create/reuse this route's own archive folder."""
    store = service.store
    if row["state"] == "ready":
        return row, None
    if row["mailbox_ref"] and row["purpose"] != "outreach_archive":
        box = next((item for item in service.settings.mailboxes if item.ref == row["mailbox_ref"]), None)
        if box is None or box.business_id != row["business_id"] or (
                box.archive_only and box.sender_ref != row["sender_ref"]):
            return _save(store, row, {"state": "failed", "blocker": "mailbox_outside_business"}), None
        return _save(store, row, {"state": "ready", "blocker": None, "purpose": box.purpose}), None
    ref = archive_ref(client.client_id, row["business_id"])
    existing = next((item for item in service.settings.mailboxes if item.ref == ref), None)
    if existing is not None:
        if existing.business_id != row["business_id"] or existing.sender_ref != row["sender_ref"]:
            return _save(store, row, {"state": "failed", "blocker": "mailbox_outside_business"}), None
        return _save(store, row, {"state": "ready", "blocker": None, "mailbox_ref": ref,
            "purpose": "outreach_archive", "folder_id": existing.folder_id}), None
    if not row["folder_name"]:
        # The random token is saved first; only this route's own create can carry it.
        name = f"Outreach archive {row['business_id']} {secrets.token_hex(6)}"
        row = _save(store, row, {"folder_name": name, "mailbox_ref": ref, "purpose": "outreach_archive"})
    operation = None
    folder_id = row["folder_id"]
    if not folder_id:
        try:
            folder_id, operation, row = _folder(service, row)
        except ProviderError as exc:
            return _save(store, row, {"state": "uncertain" if exc.uncertain else "failed",
                "blocker": "archive_folder_" + ("uncertain" if exc.uncertain else "unavailable")}), operation
        if folder_id is None:
            return row, operation
    box = Mailbox(ref=ref, folder_id=folder_id, business_id=row["business_id"], purpose="outreach_archive",
        sender_ref=row["sender_ref"])
    with store.connect() as db:
        managed = db.execute("SELECT revision FROM managed_mailboxes WHERE ref=?", (ref,)).fetchone()
    try:
        register(service, box, managed[0] if managed else 0)
    except (ValueError, ProviderError):
        return _save(store, row, {"state": "failed", "blocker": "archive_folder_invalid"}), operation
    return _save(store, row, {"state": "ready", "blocker": None}), operation


def _folder(service, row):
    """Adopt the exact named folder or create it once per recorded attempt."""
    store = service.store
    matches = [item for item in service.provider.account_folders()
        if item.get("name") == row["folder_name"] and not item.get("parentId")]
    if len(matches) > 1:
        return None, None, _save(store, row, {"state": "failed", "blocker": "archive_folder_ambiguous"})
    if matches:
        return matches[0]["id"], None, _save(store, row, {"folder_id": matches[0]["id"]})
    attempt = row["folder_attempts"] + 1
    row = _save(store, row, {"folder_attempts": attempt})
    account = AccountMailService(service.settings, store, service.provider)
    request_id = f"{row['route_id']}-folder-{attempt}"
    receipt = account.apply(AccountAction(request_id=request_id, account_id=service.provider.account_id,
        action="folder_create", name=row["folder_name"]))
    operation = {"request_id": request_id, "state": receipt["state"]}
    if receipt["state"] == "completed":
        folder = (receipt["result"].get("folder") or {}).get("id")
        if isinstance(folder, str) and folder:
            return folder, operation, _save(store, row, {"folder_id": folder})
    if receipt["state"] == "uncertain":
        return None, operation, _save(store, row, {"state": "uncertain", "blocker": "archive_folder_uncertain"})
    # A sibling-name rejection can mean an earlier uncertain create landed; look again.
    matches = [item for item in service.provider.account_folders()
        if item.get("name") == row["folder_name"] and not item.get("parentId")]
    if len(matches) == 1:
        return matches[0]["id"], operation, _save(store, row, {"folder_id": matches[0]["id"]})
    return None, operation, _save(store, row, {"state": "failed", "blocker": "archive_folder_unavailable"})


def deactivate(service, request, *, digest_source=None):
    client_for(service, request.client_id)
    digest = hashlib.sha256((digest_source or request).model_dump_json().encode()).hexdigest()
    with service.send_lock, service.sync_lock:
        row, _ = _begin(service, request, digest)
        cancelled = 0
        if not row["active"] and row["mailbox_ref"]:
            # Queued work stops through the durable engine cancellation path.
            with service.store.connect() as db:
                queued = [item[0] for item in db.execute("""SELECT i.request_id FROM integration_intents i
                    JOIN sends s ON s.request_id=i.request_id WHERE i.client_id=? AND i.business_id=?
                    AND i.mailbox=? AND s.state='queued' AND json_extract(s.payload,'$.sender_ref')=?""",
                    (request.client_id, request.business_id, row["mailbox_ref"], row["sender_ref"]))]
            for request_id in queued:
                result = service.store.cancel_send_if_queued(request_id, reason=REVOKED_REASON)
                cancelled += result["state"] == "canceled"
            service.store.flush_history()
        return {**view(service, row, request_id=request.request_id), "cancelled_queued": cancelled}


def submit_permitted(service, client, mailbox_ref, business_id, sender_ref):
    """A managed route decides its own mailbox/sender binding; otherwise static scope applies."""
    if client.business_ids and business_id not in client.business_ids:
        return False
    row = route(service.store, client.client_id, business_id)
    if (row and row["mailbox_ref"] == mailbox_ref and sender_ref is not None
            and row["sender_ref"] == sender_ref):
        # Takes precedence over a static grant of the same mailbox, so shutdown always wins.
        return bool(client.managed_routes and row["active"] and row["state"] == "ready")
    return mailbox_ref in client.allowed_mailboxes


def mailbox_access(service, client, mailbox_ref):
    """'full' for static or active routes; 'correlated' keeps revoked routes finishable."""
    if mailbox_ref in client.allowed_mailboxes:
        return "full"
    if not client.managed_routes:
        return None
    rows = [row for row in routes_for(service.store, client.client_id) if row["mailbox_ref"] == mailbox_ref]
    if any(row["active"] and row["state"] == "ready" for row in rows):
        return "full"
    return "correlated" if rows else None


def handoff_scope(service, client):
    """Mailboxes and businesses a handoff may name, including revoked routes' history."""
    mailboxes, businesses = list(client.allowed_mailboxes), list(client.business_ids)
    if client.managed_routes:
        for row in routes_for(service.store, client.client_id):
            if row["mailbox_ref"] and row["mailbox_ref"] not in mailboxes:
                mailboxes.append(row["mailbox_ref"])
            if businesses and row["business_id"] not in businesses:
                businesses.append(row["business_id"])
    return mailboxes, businesses


def listing(service, client_id):
    return {"routes": [view(service, row) for row in routes_for(service.store, client_id)]}


__all__ = ["RouteConflict", "RouteDeactivate", "RouteEnsure", "RouteRejected", "archive_ref", "deactivate",
    "ensure", "handoff_scope", "listing", "mailbox_access", "route", "route_id", "submit_permitted", "view"]
