"""Public catalog reads (``layer.catalog``): list/filter, read entry or resource, validate."""

from __future__ import annotations

import base64
from typing import Any

from .catalog import Selection, load_catalog
from .core import LayerContext
from .errors import InvalidRequestError, NotFoundError, UnavailableError
from .files import DiskSource, split_path
from .schemas import ENTRY_KINDS
from .util import is_id, is_ref, sha256_hex, split_frontmatter

MAX_RESOURCE_BYTES = 1024 * 1024
MAX_ISSUES = 100


class CatalogReader:
    def __init__(self, ctx: LayerContext) -> None:
        self.ctx = ctx

    def _load(self):
        return load_catalog(DiskSource(self.ctx.tree))

    def list(
        self,
        selection: dict[str, Any] | None = None,
        query: str | None = None,
        kinds: list[str] | None = None,
        package_ids: list[str] | None = None,
        include_unavailable: bool = False,
        include_inapplicable: bool = False,
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List eligible catalog entries (metadata only), optionally filtered by text.

        Listing without ``query`` always returns every eligible entry, so routing
        never depends on a keyword match. Global and matching scoped entries are
        both returned; overlapping entries are exposed, not ranked.
        """
        chosen = Selection.from_value(selection)
        if query is not None and not isinstance(query, str):
            raise InvalidRequestError("query must be a string")
        for field, values in (("kinds", kinds), ("package_ids", package_ids)):
            if values is not None and (not isinstance(values, list) or not all(isinstance(v, str) for v in values)):
                raise InvalidRequestError(f"{field} must be a list of strings")
        if kinds is not None and any(kind not in ENTRY_KINDS for kind in kinds):
            raise InvalidRequestError("unknown catalog kind")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise InvalidRequestError("limit must be an integer between 1 and 1000")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise InvalidRequestError("offset must be a non-negative integer")
        with self.ctx.store.lock(exclusive=False):
            catalog = self._load()
            found = catalog.search(
                chosen, query=query, kinds=kinds, package_ids=package_ids,
                include_unavailable=include_unavailable, include_inapplicable=include_inapplicable,
            )
        page = found[offset : offset + limit]
        issues = [issue.to_dict() for issue in catalog.issues]
        return {
            "catalog_revision": catalog.revision,
            "selection": chosen.to_dict(),
            "entries": page,
            "total": len(found),
            "offset": offset,
            "truncated": offset + len(page) < len(found),
            "issues": issues[:MAX_ISSUES],
            "issues_truncated": len(issues) > MAX_ISSUES,
            "recovery_required": bool(self.ctx.pending_change_ids()),
        }

    def read_entry(
        self,
        ref: str,
        selection: dict[str, Any] | None = None,
        include_dependency_bodies: bool = False,
    ) -> dict[str, Any]:
        """Load one entry body with its resources and its (transitive) dependencies."""
        if not is_ref(ref):
            raise InvalidRequestError("ref must be package-id/entry-id", details={"field": "ref"})
        chosen = Selection.from_value(selection)
        with self.ctx.store.lock(exclusive=False):
            catalog = self._load()
            entry = catalog.get(ref)
            if entry is None:
                raise NotFoundError(f"no valid catalog entry {ref!r}", details={"ref": ref,
                                    "issues": [i.to_dict() for i in catalog.issues if i.entry_ref == ref][:20]})
            described = catalog.describe(entry, chosen)
            if not described["available"]:
                raise UnavailableError(f"entry {ref} is unavailable: {'; '.join(described['diagnostics'])}",
                                       details={"ref": ref, "diagnostics": described["diagnostics"]})
            result = {
                "entry": described,
                "resources": [r.to_dict() for r in entry.resources],
                "catalog_revision": catalog.revision,
            }
            result.update(self._body(catalog, entry))
            if entry.kind in ("tool", "workflow"):
                result["definition"] = entry.definition
            dependencies = []
            for dep_ref in catalog.dependency_closure(ref):
                dep = catalog.entries[dep_ref]
                item: dict[str, Any] = {"entry": catalog.describe(dep, chosen)}
                if include_dependency_bodies:
                    item.update(self._body(catalog, dep))
                dependencies.append(item)
            result["dependencies"] = dependencies
        return result

    @staticmethod
    def _body(catalog, entry) -> dict[str, Any]:
        data = catalog.source.read(entry.root_path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"body": None, "body_base64": base64.b64encode(data).decode("ascii"), "frontmatter": None}
        frontmatter = split_frontmatter(text)[0] if entry.kind == "skill" else None
        return {"body": text, "body_base64": None, "frontmatter": frontmatter}

    def read_resource(
        self,
        package_id: str,
        path: str,
        selection: dict[str, Any] | None = None,
        max_bytes: int = MAX_RESOURCE_BYTES,
    ) -> dict[str, Any]:
        """Read any file of an enabled package by its package-relative path."""
        if not is_id(package_id):
            raise InvalidRequestError("package_id is not a valid ID", details={"field": "package_id"})
        split_path(path, field="path")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 64 * 1024 * 1024:
            raise InvalidRequestError("max_bytes must be an integer between 1 and 67108864")
        chosen = Selection.from_value(selection)
        if chosen.package_ids is not None and package_id not in chosen.package_ids:
            raise UnavailableError(f"package {package_id!r} is not enabled for this selection",
                                   details={"package_id": package_id,
                                            "diagnostics": [f"package {package_id!r} is not enabled for this selection"]})
        root_path = f"packages/{package_id}/{path}"
        with self.ctx.store.lock(exclusive=False):
            data = self.ctx.tree.read(root_path, max_bytes=max_bytes)
        if data is None:
            raise NotFoundError(f"no such resource {path!r} in package {package_id!r}",
                                details={"package_id": package_id, "path": path})
        try:
            text, encoding = data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, encoding = base64.b64encode(data).decode("ascii"), "base64"
        return {"package_id": package_id, "path": path, "root_path": root_path, "sha256": sha256_hex(data),
                "size": len(data), "encoding": encoding, "content": text}

    def validate(self, package_ids: list[str] | None = None) -> dict[str, Any]:
        """Validate the whole catalog; ``package_ids`` only narrows what is reported."""
        if package_ids is not None and (not isinstance(package_ids, list) or not all(is_id(v) for v in package_ids)):
            raise InvalidRequestError("package_ids must be a list of valid IDs")
        with self.ctx.store.lock(exclusive=False):
            catalog = self._load()
        wanted = set(package_ids) if package_ids else None
        issues = [i for i in catalog.issues if wanted is None or i.package_id in wanted]
        packages = [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "source": p.source,
                "manifest_sha256": p.manifest_sha256,
                "entry_ids": p.entry_ids,
            }
            for p in sorted(catalog.packages.values(), key=lambda p: p.id)
            if wanted is None or p.id in wanted
        ]
        return {
            "valid": not any(i.severity == "error" for i in issues),
            "issues": [i.to_dict() for i in issues][:MAX_ISSUES],
            "issues_truncated": len(issues) > MAX_ISSUES,
            "packages": packages,
            "entry_count": len(catalog.entries),
            "catalog_revision": catalog.revision,
        }
