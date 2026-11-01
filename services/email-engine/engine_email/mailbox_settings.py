"""Durable mailbox registration for the trusted business-settings service."""

from dataclasses import replace
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from .models import Mailbox


class MailboxUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,120}$")
    mailbox: Mailbox
    expected_revision: int = Field(ge=0)


def load_settings(settings, store):
    with store.connect() as db:
        managed = [Mailbox.model_validate_json(row[0]) for row in db.execute(
            "SELECT payload FROM managed_mailboxes ORDER BY ref")]
    boxes = {box.ref: box for box in settings.mailboxes}
    for box in managed:
        old = boxes.get(box.ref)
        if old and (old.business_id, old.folder_id, old.address, old.purpose, old.sender_ref) != (
                box.business_id, box.folder_id, box.address, box.purpose, box.sender_ref):
            raise ValueError("Configured mailbox conflicts with its managed ownership binding")
        boxes[box.ref] = box
    return replace(settings, mailboxes=tuple(boxes.values()))


def revisions(store):
    with store.connect() as db:
        return {row[0]: row[1] for row in db.execute("SELECT ref,revision FROM managed_mailboxes")}


def configure(service, request):
    box = request.mailbox
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    # The existing sending and sync locks protect provider routing during updates.
    with service.send_lock, service.sync_lock:
        with service.store.connect() as db:
            receipt = db.execute("SELECT digest,response FROM mailbox_setting_requests WHERE id=?",
                                 (request.request_id,)).fetchone()
            if receipt:
                if receipt[0] != digest:
                    raise ValueError("Mailbox request ID was already used for another change")
                result = json.loads(receipt[1])
                service.settings = load_settings(service.settings, service.store)
                service.provider.settings = service.settings
                return result
            old = db.execute("SELECT revision FROM managed_mailboxes WHERE ref=?", (box.ref,)).fetchone()
            revision = old[0] if old else 0
            if request.expected_revision != revision:
                raise ValueError("Mailbox revision changed; read current settings before retrying")
        if box.archive_only:
            raise ValueError("Archive-only mailboxes are created only by managed outreach routes")
        return register(service, box, revision, receipt=(request.request_id, digest))


def register(service, box, revision, *, receipt=None):
    """Validate and durably bind one mailbox; callers hold the send and sync locks."""
    boxes = {item.ref: item for item in service.settings.mailboxes}
    boxes[box.ref] = box
    updated = replace(service.settings, mailboxes=tuple(boxes.values()))
    if sum(item.business_id == box.business_id for item in updated.mailboxes) > 50:
        raise ValueError("At most 50 mailboxes may belong to a business")
    folder = service.provider.folders().get(box.folder_id)
    rights = (folder or {}).get("myRights", {})
    if (not folder or folder.get("role") or not rights.get("mayReadItems")
            or box.archive_only and not rights.get("mayAddItems")):
        raise ValueError("Mailbox requires a readable dedicated Fastmail folder")
    if not box.archive_only:
        identities = service.provider.call("Identity/get")["list"]
        for address in (box.address, *box.aliases):
            service.provider.identity(box, address, identities=identities)
    with service.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        service.store.bind_mailboxes_in(db, updated.mailboxes)
        payload = box.model_dump_json()
        db.execute("INSERT INTO managed_mailboxes VALUES(?,?,?) ON CONFLICT(ref) DO UPDATE "
                   "SET payload=excluded.payload,revision=excluded.revision", (box.ref, payload, revision + 1))
        # Replay the account listing to discover the new route. Keep existing
        # cached messages, send receipts, approvals and quota reservations.
        db.execute("UPDATE folders SET cursor=NULL WHERE mailbox='@account'")
        response = {"mailbox": box.model_dump(), "revision": revision + 1, "state": "configured"}
        if receipt is not None:
            db.execute("INSERT INTO mailbox_setting_requests VALUES(?,?,?)",
                       (receipt[0], receipt[1], json.dumps(response)))
    service.settings = updated
    service.provider.settings = updated
    return response
