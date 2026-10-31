"""Read-credential business mailbox views: search, draft listing, attachments.

This module is read-only. It never mutates provider state, writes receipts,
or extends what the read credential may change: mutations (moves, trash,
restore, flag updates, draft creation, deletes) stay on the admin-token
``/fastmail`` account surface with its ``ifInState`` pins and protected-folder
guards, with business-scope authorization enforced application-side in core.

Ownership model. A business mailbox owns exactly the mail filed in its
dedicated provider folder (the immutable folder binding). Business mail that a
workflow moved into the account's shared Drafts/Sent/Trash folders is no
longer provable from folder membership alone, and those shared folders also
hold other businesses' and the owner's personal mail. The durable ownership
record lives in core, fixed at first attribution; the engine never guesses
shared-folder ownership from headers or names. Every read that can touch a
message outside the mailbox's bound folder therefore carries explicit hints
from the trusted core service:

- ``owned_ids``: provider message ids that core's ownership record attributes
  to this mailbox's business (comma-separated in a query string; at most
  ``MAX_OWNED_IDS`` per request). A message outside the bound folder is served
  only when its id is listed here AND its current folders include no other
  business's bound folder and no private-history folder. A hint can never
  override a provable foreign folder.
- ``folder_id`` (search and draft listing): one provider folder to view
  instead of the bound folder, normally the shared Drafts/Sent/Trash. Another
  business's bound folder or a private-history folder is refused outright, and
  any non-bound folder view requires ``owned_ids``. When a shared folder is
  viewed, the provider-wide ``total`` is withheld (``None``) so foreign
  message counts do not leak; page with ``position`` until ``has_more`` is
  false.
"""

from .fastmail import ProviderError

ATTACHMENT_PAGE_BYTES = 5 * 1024 * 1024
BUSINESS_MAX_RESULTS = 100
DRAFTS_MAX_RESULTS = 50
MAX_OWNED_IDS = 500


def parse_owned_ids(value):
    """Parse comma-separated provider message ids asserted by core's record."""
    if not value:
        return []
    ids = [item for item in value.split(",") if item]
    if len(ids) > MAX_OWNED_IDS or any(len(item) > 255 for item in ids):
        raise ValueError("Invalid owned message ids")
    return list(dict.fromkeys(ids))


class BusinessMailService:
    """Mailbox-scoped provider views filtered by binding plus explicit hints."""

    def __init__(self, settings, provider):
        self.settings, self.provider = settings, provider

    def _protected(self, mailbox):
        """Folders that provably belong to someone else; a hint never opens them."""
        others = {box.folder_id for box in self.settings.mailboxes if box.ref != mailbox.ref}
        private = set(self.settings.history.private_folder_ids) if self.settings.history else set()
        return others | private

    def _owned(self, mailbox, raw, owned_ids):
        if self.provider.owner(raw) == mailbox:
            return True
        folders = raw.get("mailboxIds") or {}
        return (raw.get("id") in owned_ids
            and not any(folders.get(folder) for folder in self._protected(mailbox)))

    def _scoped_metadata(self, mailbox, identity, owned_ids=()):
        if not isinstance(identity, str) or not identity:
            raise ProviderError("message_outside_business", status=404)
        try:
            raw = self.provider.account_raw(identity, metadata=True)
        except ProviderError as exc:
            if exc.status == 404:
                # Do not reveal whether an unowned identifier exists at all.
                raise ProviderError("message_outside_business", status=404) from exc
            raise
        if not self._owned(mailbox, raw, set(owned_ids)):
            raise ProviderError("message_outside_business", status=404)
        return raw

    def _view_folder(self, mailbox, params, owned_ids):
        folder = params.get("folder_id") or mailbox.folder_id
        if folder in self._protected(mailbox):
            raise ProviderError("folder_outside_business", status=404)
        if folder != mailbox.folder_id and not owned_ids:
            raise ValueError("Shared folder views require owned message ids")
        return folder

    def _visible_page(self, mailbox, result, owned_ids):
        ids = [value for value in result.get("ids", []) if isinstance(value, str)]
        metadata = self.provider.get_raw(ids, metadata=True).get("list", []) if ids else []
        by_id = {item["id"]: item for item in metadata
            if isinstance(item, dict) and isinstance(item.get("id"), str)}
        visible = [identity for identity in ids
            if identity in by_id and self._owned(mailbox, by_id[identity], owned_ids)]
        return ids, visible

    def _page(self, params, maximum, default):
        limit = int(params.get("limit", default))
        position = int(params.get("position", 0))
        if not 1 <= limit <= maximum or position < 0:
            raise ValueError("Invalid page")
        return limit, position

    def search(self, mailbox, params):
        allowed = {"q", "from", "to", "subject", "has_keyword", "before", "after",
            "limit", "position", "folder_id", "owned_ids"}
        if set(params) - allowed:
            raise ValueError("Unsupported search parameter")
        limit, position = self._page(params, BUSINESS_MAX_RESULTS, 50)
        owned_ids = set(parse_owned_ids(params.get("owned_ids", "")))
        folder = self._view_folder(mailbox, params, owned_ids)
        query = {key: value for key, value in params.items()
            if key in {"q", "from", "to", "subject", "has_keyword", "before", "after"}
            and value not in {None, ""}}
        result = self.provider.account_search({**query, "in_mailbox": folder},
            limit=limit, position=position)
        ids, visible = self._visible_page(mailbox, result, owned_ids)
        return {"ids": visible, "position": result["position"],
            "has_more": result["position"] + len(ids) < result["total"],
            "total": result["total"] if folder == mailbox.folder_id else None,
            "query_state": result.get("query_state"), "folder_id": folder}

    def drafts(self, mailbox, params):
        allowed = {"limit", "position", "folder_id", "owned_ids"}
        if set(params) - allowed:
            raise ValueError("Unsupported draft listing parameter")
        limit, position = self._page(params, DRAFTS_MAX_RESULTS, 20)
        owned_ids = set(parse_owned_ids(params.get("owned_ids", "")))
        folder = self._view_folder(mailbox, params, owned_ids)
        result = self.provider.account_search({"in_mailbox": folder, "has_keyword": "$draft"},
            limit=limit, position=position)
        ids, visible = self._visible_page(mailbox, result, owned_ids)
        bodies = self.provider.get_raw(visible).get("list", []) if visible else []
        # Recheck after the body fetch so a move between calls is not exposed.
        drafts = [self.provider.normalize(item) for item in bodies
            if isinstance(item, dict) and self._owned(mailbox, item, owned_ids)]
        return {"drafts": drafts, "position": result["position"],
            "has_more": result["position"] + len(ids) < result["total"],
            "total": result["total"] if folder == mailbox.folder_id else None,
            "query_state": result.get("query_state"), "folder_id": folder}

    def attachments(self, mailbox, identity, owned_ids=()):
        raw = self._scoped_metadata(mailbox, identity, owned_ids)
        return [{"id": part.get("blobId"), "name": part.get("name") or "attachment",
            "contentType": part.get("type") or "application/octet-stream",
            "size": part.get("size", 0), "isInline": part.get("disposition") == "inline"}
            for part in raw.get("attachments") or []]

    def attachment(self, mailbox, identity, attachment_id, owned_ids=()):
        selected = next((item for item in self.attachments(mailbox, identity, owned_ids)
            if item["id"] == attachment_id), None)
        if selected is None:
            raise ProviderError("attachment_outside_business", status=404)
        return self.provider.download_blob(attachment_id)
