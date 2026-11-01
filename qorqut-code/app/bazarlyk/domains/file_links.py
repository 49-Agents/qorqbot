"""files_link: build an ordinary viewer link to a file, without reading it.

A path selector is checked lexically only (see ``bazarlyk.file_viewer.paths``):
no bytes, stat, existence or directory listing, so a link grants an agent no
file access and leaks nothing about the filesystem. A document selector goes
through the existing caller-scoped document read rules, so a foreign or
inaccessible document ID is refused. The owner's viewer checks the filesystem
when the link is opened; the link itself is root-relative and works on whichever
origin the owner uses.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from uuid import UUID as _UUID

from bazarlyk.application import OperationDescriptor
from bazarlyk.contracts import DomainError
from bazarlyk.file_viewer.paths import MAX_LABEL_CHARS, MAX_PATH_CHARS, markdown_link, normalize_path, viewer_url

from .common import UUID, schema

DESCRIPTION = (
    "Build an ordinary link the owner can click to read a file in the Qorqut file viewer. Give exactly one of path "
    "(absolute under the businesses root, or relative to it) or document_id (a registered document readable in your "
    "current scope; Global agents select a business with business_id as usual). Optional label; optional version "
    "(document_id only) links one saved version instead of the current file. Returns viewer_url (root-relative) and a "
    "ready Markdown link to paste into your reply. For a path this only builds text: nothing is read, listed or "
    "verified, and the viewer checks the file when the owner opens it, so never claim the file exists because a link "
    "was built. Grants no file access; only the owner's viewer reads content. Read-only.")


def namespace(ctx, _arguments):
    """Any active caller: its business, else Global when granted, else its own agent workspace."""
    if ctx.business_id:
        return "business:" + ctx.business_id
    if "read_global_context" in ctx.global_capabilities:
        return "global"
    if ctx.agent_id:
        return "agent:" + ctx.agent_id
    raise DomainError("scope_required", "Select a business or Global scope")


class FileLinks:
    def __init__(self, documents, businesses_root):
        self.documents = documents
        root = Path(businesses_root)
        self.roots = tuple(dict.fromkeys((str(root), str(root.resolve()))))

    def register(self, registry):
        registry.register(OperationDescriptor("files_link", schema({
            "path": {"type": "string", "minLength": 1, "maxLength": MAX_PATH_CHARS},
            "document_id": UUID,
            "label": {"type": "string", "minLength": 1, "maxLength": MAX_LABEL_CHARS},
            "version": {"type": "integer", "minimum": 1, "maximum": 999_999_999}}),
            effect_kind="read", supports_signoff=False, exposure=frozenset({"mcp", "dashboard"}),
            scope_kinds=frozenset({"business", "global", "agent_workspace"}), global_capability="read_global_context",
            resolve_namespace=namespace, description=DESCRIPTION, read=self.read))
        return self

    def read(self, ctx, args, options, uow):
        if ("path" in args) == ("document_id" in args):
            raise DomainError("validation_error", "Give exactly one of path or document_id")
        if "path" in args:
            if "version" in args:
                raise DomainError("validation_error", "version applies only to a document_id link")
            relative = normalize_path(args["path"], self.roots)
            url = viewer_url(path=relative)
            label = args.get("label") or posixpath.basename(relative)
            return {"viewer_url": url, "markdown": markdown_link(label, url), "target": {"type": "path", "path": relative},
                    "checked": "not_checked",
                    "note": "Built from the path text alone; nothing was read, listed or verified. The owner's viewer "
                            "checks the file when the link is opened and always shows its current contents."}
        identity = str(_UUID(args["document_id"]))
        row = self.documents.store.get(ctx, identity, uow)
        if row["business_id"] != ctx.business_id:
            raise DomainError("not_found", "Document not found")
        version = args.get("version")
        url = viewer_url(document_id=row["id"], business_id=ctx.business_id, version=version)
        label = args.get("label") or posixpath.basename(row["path"])
        return {"viewer_url": url, "markdown": markdown_link(label, url),
                "target": {"type": "document", "document_id": row["id"], "business_id": ctx.business_id, "version": version},
                "checked": "document_readable",
                "note": ("The document is registered and readable in your scope; the owner's viewer shows its current "
                         "file" + (f" and saved version {version}, which is checked when opened." if version else
                                   " and its saved versions. The link survives the document being moved."))}
