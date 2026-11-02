"""Expected-hash batch mutation with a durable pending-change journal.

Write path (all under the exclusive authoring lock)::

    build ops -> record unobserved external edits to the affected paths
      -> check expected hashes -> validate the projected catalog
      -> journal before/after contents (SQLite, durable)
      -> atomically replace each file
      -> finalize: event + inventory in one transaction, drop the journal

A crash between journaling and finalizing leaves a pending change that
``recover`` completes only when every affected file still matches its recorded
before or after hash.
"""

from __future__ import annotations

import base64
import binascii
import os
import stat
from dataclasses import dataclass, field
from typing import Any, Callable

from .catalog import MANIFEST_NAME, Catalog, bundle_digest, load_catalog
from .layout import import_dir, require_editable
from .changes import EXTERNAL_ATTRIBUTION, Changes, event_dict, insert_event, normalize_attribution, package_ids_of
from .core import LayerContext
from .errors import (
    AgenticError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    RecoveryConflictError,
    RecoveryRequiredError,
    UnsafePathError,
    ValidationFailedError,
)
from .files import DiskSource, OverlaySource, split_path
from .util import dump_yaml, dumps, is_id, load_yaml, loads, new_id, sha256_hex

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_BATCH_FILES = 1000
SHA_LEN = 64


@dataclass
class FileOp:
    path: str
    action: str  # create | edit | delete
    content: bytes | None
    expected_sha256: str | None
    executable: bool | None = None


@dataclass
class Batch:
    kind: str
    ops: list[FileOp]
    details: dict[str, Any] = field(default_factory=dict)
    source_refs: list[str] = field(default_factory=list)


@dataclass
class Planned:
    path: str
    action: str
    before: bytes | None
    after: bytes | None
    mode: int | None

    @property
    def before_sha(self) -> str | None:
        return None if self.before is None else sha256_hex(self.before)

    @property
    def after_sha(self) -> str | None:
        return None if self.after is None else sha256_hex(self.after)


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == SHA_LEN and all(c in "0123456789abcdef" for c in value)


def _decode_content(item: dict[str, Any], index: int) -> bytes:
    has_text, has_b64 = "content" in item, "content_base64" in item
    if has_text == has_b64:
        raise InvalidRequestError(f"changes[{index}] needs exactly one of content or content_base64")
    if has_text:
        if not isinstance(item["content"], str):
            raise InvalidRequestError(f"changes[{index}].content must be a string")
        return item["content"].encode("utf-8")
    try:
        if not isinstance(item["content_base64"], str):
            raise TypeError
        return base64.b64decode(item["content_base64"], validate=True)
    except (binascii.Error, TypeError, ValueError):
        raise InvalidRequestError(f"changes[{index}].content_base64 is not valid base64") from None


def parse_changes(changes: Any) -> list[FileOp]:
    if not isinstance(changes, list) or not changes:
        raise InvalidRequestError("changes must be a non-empty list")
    if len(changes) > MAX_BATCH_FILES:
        raise AgenticError(f"at most {MAX_BATCH_FILES} file changes per batch", code="limit_exceeded",
                           details={"limit": MAX_BATCH_FILES})
    ops: list[FileOp] = []
    seen: set[str] = set()
    total = 0
    for index, item in enumerate(changes):
        if not isinstance(item, dict):
            raise InvalidRequestError(f"changes[{index}] must be an object")
        unknown = set(item) - {"op", "path", "content", "content_base64", "expected_sha256", "executable"}
        if unknown:
            raise InvalidRequestError(f"changes[{index}] has unknown field(s): {sorted(unknown)}")
        action = item.get("op")
        if action not in ("create", "edit", "delete"):
            raise InvalidRequestError(f"changes[{index}].op must be create, edit or delete")
        path = item.get("path")
        split_path(path, field=f"changes[{index}].path")
        if path in seen:
            raise InvalidRequestError(f"changes[{index}] repeats path {path!r}; one change per path per batch")
        seen.add(path)
        expected = item.get("expected_sha256")
        if action == "create":
            if expected is not None:
                raise InvalidRequestError(f"changes[{index}]: create expects the path to be absent; omit expected_sha256")
        elif not _is_sha(expected):
            raise InvalidRequestError(f"changes[{index}].expected_sha256 is required (64 lowercase hex characters)")
        content = None
        if action != "delete":
            content = _decode_content(item, index)
            if len(content) > MAX_FILE_BYTES:
                raise AgenticError(f"changes[{index}] is larger than {MAX_FILE_BYTES} bytes", code="limit_exceeded",
                                   details={"path": path, "limit": MAX_FILE_BYTES})
            total += len(content)
        elif "content" in item or "content_base64" in item or "executable" in item:
            raise InvalidRequestError(f"changes[{index}]: delete takes no content")
        executable = item.get("executable")
        if executable is not None and not isinstance(executable, bool):
            raise InvalidRequestError(f"changes[{index}].executable must be a boolean")
        ops.append(FileOp(path, action, content, expected, executable))
    if total > MAX_BATCH_BYTES:
        raise AgenticError(f"batch content exceeds {MAX_BATCH_BYTES} bytes", code="limit_exceeded",
                           details={"limit": MAX_BATCH_BYTES})
    return ops


def package_revision(files: dict[str, Any], package_dir: str) -> str:
    prefix = package_dir + "/"
    return bundle_digest((rel, entry.sha256) for rel, entry in files.items() if rel.startswith(prefix))


def _changed_entries(before: Catalog, after: Catalog) -> list[dict[str, str]]:
    result = []
    for ref in sorted(set(before.entries) | set(after.entries)):
        old, new = before.entries.get(ref), after.entries.get(ref)
        if old is None and new is not None:
            state = "added"
        elif old is not None and new is None:
            state = "removed"
        elif old is not None and new is not None and old != new:
            state = "changed"
        else:
            continue
        result.append(
            {
                "ref": ref,
                "state": state,
                "revision": new.revision if new else None,
                "bundle_revision": new.bundle_revision if new else None,
            }
        )
    return result


class Authoring:
    """Public authoring operations (``layer.authoring``)."""

    def __init__(self, ctx: LayerContext, changes: Changes) -> None:
        self.ctx = ctx
        self.changes = changes

    # ------------------------------------------------------------------ apply
    def apply(self, attribution: dict[str, Any], changes: list[dict[str, Any]], dry_run: bool = False) -> dict[str, Any]:
        """Apply creates/edits/deletes as one validated, journaled, evented batch."""
        ops = parse_changes(changes)
        return self._run("apply", attribution, lambda _ctx: Batch("apply", ops), dry_run=dry_run)

    # ----------------------------------------------------------------- import
    def import_package(
        self,
        attribution: dict[str, Any],
        package_id: str,
        snapshot: dict[str, Any],
        manifest: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        expected_package_revision: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Copy a snapshot into its editable plugin or singleton preference directory.

        The snapshot is a directory path or an in-memory file list; bytes are
        copied, never linked. Importing over an existing package requires its
        current ``expected_package_revision`` and makes the package match the
        snapshot (files absent from the snapshot are deleted).
        """
        if not is_id(package_id):
            raise InvalidRequestError("package_id is not a valid ID", details={"field": "package_id"})
        files = read_snapshot(snapshot)
        digest = bundle_digest((rel, sha256_hex(data)) for rel, (data, _mode) in files.items())
        if manifest is not None:
            if not isinstance(manifest, dict):
                raise InvalidRequestError("manifest must be an object")
            if MANIFEST_NAME in files:
                raise InvalidRequestError(f"supply the manifest either as {MANIFEST_NAME} in the snapshot or as manifest, not both")
            files[MANIFEST_NAME] = (dump_yaml(manifest).encode("utf-8"), 0o644)
        if MANIFEST_NAME not in files:
            raise ValidationFailedError(f"the snapshot has no {MANIFEST_NAME}; supply one or pass manifest",
                                        details={"issues": [f"missing {MANIFEST_NAME}"]})
        if provenance is not None:
            if not isinstance(provenance, dict):
                raise InvalidRequestError("provenance must be an object")
            try:
                data = load_yaml(files[MANIFEST_NAME][0].decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValidationFailedError(f"{MANIFEST_NAME} could not be read: {exc}", details={"issues": [str(exc)]}) from exc
            if not isinstance(data, dict):
                raise ValidationFailedError(f"{MANIFEST_NAME} must be a mapping", details={"issues": ["not a mapping"]})
            source = dict(data.get("source") or {})
            source.update(provenance)
            source["snapshot_digest"] = digest
            source["file_count"] = len(files) if manifest is None else len(files) - 1
            data["source"] = source
            files[MANIFEST_NAME] = (dump_yaml(data).encode("utf-8"), files[MANIFEST_NAME][1])

        def build(bctx: BuildContext) -> Batch:
            # Even invalid or ambiguous protected packages reserve their IDs.
            protected = [f"{area}/{package_id}/{MANIFEST_NAME}" for area in ("roles", "operations", "overseer")
                         if any(rel == f"{area}/{package_id}" or rel.startswith(f"{area}/{package_id}/")
                                for rel in (*bctx.base.files, *bctx.base.special))]
            if protected:
                require_editable(protected)
            prefix = (bctx.catalog.packages[package_id].dir if package_id in bctx.catalog.packages else import_dir(package_id)) + "/"
            require_editable([prefix + MANIFEST_NAME])
            existing = {rel: e for rel, e in bctx.base.files.items() if rel.startswith(prefix)}
            current_revision = package_revision(bctx.base.files, prefix.rstrip("/")) if existing else None
            if existing:
                if expected_package_revision is None:
                    raise ConflictError(
                        f"package {package_id!r} already exists; pass its current revision to update it",
                        details={"conflicts": [{"path": prefix.rstrip("/"), "reason": "package_exists",
                                                "current_package_revision": current_revision}]},
                    )
                if expected_package_revision != current_revision:
                    raise ConflictError(
                        f"package {package_id!r} changed since revision {expected_package_revision}",
                        details={"conflicts": [{"path": prefix.rstrip("/"), "reason": "revision_mismatch",
                                                "expected_package_revision": expected_package_revision,
                                                "current_package_revision": current_revision}]},
                    )
            elif expected_package_revision is not None:
                raise ConflictError(
                    f"package {package_id!r} does not exist, so there is no revision to update",
                    details={"conflicts": [{"path": prefix.rstrip("/"), "reason": "missing"}]},
                )
            ops: list[FileOp] = []
            for rel, (data, mode) in sorted(files.items()):
                path = prefix + rel
                current = existing.get(path)
                if current is None:
                    ops.append(FileOp(path, "create", data, None, bool(mode & 0o100)))
                elif current.sha256 != sha256_hex(data):
                    ops.append(FileOp(path, "edit", data, current.sha256, bool(mode & 0o100)))
            for path, current in sorted(existing.items()):
                if path[len(prefix):] not in files:
                    ops.append(FileOp(path, "delete", None, current.sha256))
            return Batch(
                "import",
                ops,
                details={
                    "package_id": package_id,
                    "mode": "update" if existing else "create",
                    "previous_package_revision": current_revision,
                    "snapshot_digest": digest,
                    "file_count": len(files),
                    "provenance": provenance,
                },
            )

        result = self._run("import", attribution, build, dry_run=dry_run)
        result["snapshot_digest"] = digest
        return result

    # ---------------------------------------------------------- entry/package
    def update_entry(
        self,
        attribution: dict[str, Any],
        package_id: str,
        entry: dict[str, Any],
        expected_manifest_sha256: str,
        content: str | None = None,
        content_base64: str | None = None,
        expected_sha256: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create or update one manifest entry (and optionally its file) in one batch.

        ``entry`` holds manifest fields (``id`` required); given fields replace
        the stored ones. The manifest is rewritten as normalized YAML.
        """
        if not isinstance(entry, dict) or not is_id(entry.get("id")):
            raise InvalidRequestError("entry must be an object with a valid id")
        if not _is_sha(expected_manifest_sha256):
            raise InvalidRequestError("expected_manifest_sha256 is required")
        if content is not None and content_base64 is not None:
            raise InvalidRequestError("give content or content_base64, not both")
        payload: bytes | None = None
        if content is not None or content_base64 is not None:
            payload = _decode_content({k: v for k, v in (("content", content), ("content_base64", content_base64)) if v is not None}, 0)
        if expected_sha256 is not None and not _is_sha(expected_sha256):
            raise InvalidRequestError("expected_sha256 must be 64 lowercase hex characters")

        def build(bctx: BuildContext) -> Batch:
            _data, manifest_path, manifest = self._manifest_for_edit(bctx, package_id, expected_manifest_sha256)
            entries = manifest["entries"]
            index = next((i for i, e in enumerate(entries) if e.get("id") == entry["id"]), None)
            merged = dict(entries[index]) if index is not None else {}
            merged.update(entry)
            if index is None:
                entries.append(merged)
            else:
                entries[index] = merged
            ops = [FileOp(manifest_path, "edit", dump_yaml(manifest).encode("utf-8"), expected_manifest_sha256)]
            if payload is not None:
                if not isinstance(merged.get("path"), str):
                    raise InvalidRequestError("entry.path is required to write content")
                split_path(merged["path"], field="entry.path")
                target = f"{manifest_path.rsplit('/', 1)[0]}/{merged['path']}"
                current = bctx.base.files.get(target)
                if current is None:
                    if expected_sha256 is not None:
                        raise ConflictError("the entry file does not exist", details={"conflicts": [
                            {"path": target, "reason": "missing", "expected_sha256": expected_sha256}]})
                    ops.append(FileOp(target, "create", payload, None))
                else:
                    if expected_sha256 is None:
                        raise ConflictError("the entry file exists; pass its expected_sha256 to replace it", details={
                            "conflicts": [{"path": target, "reason": "already_exists", "current_sha256": current.sha256}]})
                    ops.append(FileOp(target, "edit", payload, expected_sha256))
            return Batch("update_entry", ops, details={"package_id": package_id, "entry_id": entry["id"],
                                                        "created_entry": index is None})

        return self._run("update_entry", attribution, build, dry_run=dry_run)

    def remove_entry(
        self,
        attribution: dict[str, Any],
        package_id: str,
        entry_id: str,
        expected_manifest_sha256: str,
        delete_files: bool = False,
        expected_bundle_revision: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Remove a manifest entry; optionally delete its file and resources.

        Deleting files requires the entry's current ``expected_bundle_revision``.
        Validation refuses removal while another entry still requires this one.
        """
        if not _is_sha(expected_manifest_sha256):
            raise InvalidRequestError("expected_manifest_sha256 is required")
        if delete_files and not _is_sha(expected_bundle_revision):
            raise InvalidRequestError("expected_bundle_revision is required when delete_files is true")

        def build(bctx: BuildContext) -> Batch:
            _data, manifest_path, manifest = self._manifest_for_edit(bctx, package_id, expected_manifest_sha256)
            kept = [e for e in manifest["entries"] if e.get("id") != entry_id]
            if len(kept) == len(manifest["entries"]):
                raise NotFoundError(f"entry {package_id}/{entry_id} is not in the manifest", details={"ref": f"{package_id}/{entry_id}"})
            manifest["entries"] = kept
            ops = [FileOp(manifest_path, "edit", dump_yaml(manifest).encode("utf-8"), expected_manifest_sha256)]
            if delete_files:
                ref = f"{package_id}/{entry_id}"
                target = bctx.catalog.get(ref)
                if target is None:
                    raise NotFoundError(f"entry {ref} is not a valid catalog entry, so its files cannot be resolved",
                                        details={"ref": ref})
                if target.bundle_revision != expected_bundle_revision:
                    raise ConflictError(
                        f"entry {ref} changed since bundle revision {expected_bundle_revision}",
                        details={"conflicts": [{"path": target.root_path, "reason": "revision_mismatch",
                                                "expected_bundle_revision": expected_bundle_revision,
                                                "current_bundle_revision": target.bundle_revision}]},
                    )
                shared = {target.root_path, *(r.root_path for r in target.resources)}
                for other in bctx.catalog.entries.values():
                    if other.ref != ref:
                        shared -= {other.root_path, *(r.root_path for r in other.resources)}
                for path in sorted(shared):
                    ops.append(FileOp(path, "delete", None, bctx.base.files[path].sha256))
            return Batch("remove_entry", ops, details={"package_id": package_id, "entry_id": entry_id,
                                                        "deleted_files": delete_files})

        return self._run("remove_entry", attribution, build, dry_run=dry_run)

    def remove_package(
        self,
        attribution: dict[str, Any],
        package_id: str,
        expected_package_revision: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete every file of a package, given its current revision."""
        if not is_id(package_id):
            raise InvalidRequestError("package_id is not a valid ID", details={"field": "package_id"})
        if not _is_sha(expected_package_revision):
            raise InvalidRequestError("expected_package_revision is required")

        def build(bctx: BuildContext) -> Batch:
            prefix = (bctx.catalog.packages[package_id].dir if package_id in bctx.catalog.packages else import_dir(package_id)) + "/"
            require_editable([prefix + MANIFEST_NAME])
            existing = {rel: e for rel, e in bctx.base.files.items() if rel.startswith(prefix)}
            if not existing:
                raise NotFoundError(f"package {package_id!r} does not exist", details={"package_id": package_id})
            current = package_revision(bctx.base.files, prefix.rstrip("/"))
            if current != expected_package_revision:
                raise ConflictError(
                    f"package {package_id!r} changed since revision {expected_package_revision}",
                    details={"conflicts": [{"path": prefix.rstrip("/"), "reason": "revision_mismatch",
                                            "expected_package_revision": expected_package_revision,
                                            "current_package_revision": current}]},
                )
            ops = [FileOp(path, "delete", None, info.sha256) for path, info in sorted(existing.items())]
            return Batch("remove_package", ops, details={"package_id": package_id, "previous_package_revision": current})

        return self._run("remove_package", attribution, build, dry_run=dry_run)

    @staticmethod
    def _manifest_for_edit(bctx: "BuildContext", package_id: str, expected_sha: str) -> tuple[bytes, str, dict[str, Any]]:
        if not is_id(package_id):
            raise InvalidRequestError("package_id is not a valid ID", details={"field": "package_id"})
        package = bctx.catalog.packages.get(package_id)
        path = f"{package.dir if package else import_dir(package_id)}/{MANIFEST_NAME}"
        require_editable([path])
        info = bctx.base.files.get(path)
        if info is None:
            raise NotFoundError(f"package {package_id!r} has no {MANIFEST_NAME}", details={"package_id": package_id})
        if info.sha256 != expected_sha:
            raise ConflictError(
                f"the manifest of {package_id!r} changed since it was read",
                details={"conflicts": [{"path": path, "reason": "hash_mismatch", "expected_sha256": expected_sha,
                                        "current_sha256": info.sha256}]},
            )
        data = bctx.base.read(path)
        try:
            manifest = load_yaml(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationFailedError(f"the manifest of {package_id!r} cannot be edited: {exc}",
                                        details={"issues": [str(exc)]}) from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
            raise ValidationFailedError(f"the manifest of {package_id!r} has no entries list",
                                        details={"issues": ["entries is not a list"]})
        return data, path, manifest

    # ------------------------------------------------------------------ engine
    def _run(
        self,
        kind: str,
        attribution: dict[str, Any],
        build: Callable[["BuildContext"], Batch],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        ctx = self.ctx
        attr = normalize_attribution(attribution, trust=ctx.attribution_source)
        with ctx.store.lock(exclusive=True):
            ctx.require_no_recovery()
            ctx.require_no_check_mutation()
            base = DiskSource(ctx.tree)
            before_catalog = load_catalog(base)
            batch = build(BuildContext(base=base, catalog=before_catalog))
            # All entry/package helpers share the same bounds as raw apply.
            if len(batch.ops) > MAX_BATCH_FILES or sum(len(op.content or b"") for op in batch.ops) > MAX_BATCH_BYTES:
                raise AgenticError("batch exceeds authoring limits", code="limit_exceeded")
            if any(len(op.content or b"") > MAX_FILE_BYTES for op in batch.ops):
                raise AgenticError("file exceeds authoring limits", code="limit_exceeded")
            for op in batch.ops:
                ctx.tree.kind(op.path)  # Detect unsafe parent symlinks before policy classification.
            require_editable(op.path for op in batch.ops)
            if not dry_run:
                self._record_unobserved_edits({op.path for op in batch.ops})
            planned, unchanged = self._plan(batch.ops)
            overlay = OverlaySource(base, {p.path: p.after for p in planned})
            projected = load_catalog(overlay)
            known = {issue.key() for issue in before_catalog.errors()}
            introduced = [issue for issue in projected.errors() if issue.key() not in known]
            if introduced:
                raise ValidationFailedError(
                    "the change would make the catalog invalid; nothing was written",
                    details={"issues": [issue.to_dict() for issue in introduced], "changed": False},
                )
            entries = _changed_entries(before_catalog, projected)
            summary = {
                "dry_run": dry_run,
                "files": [
                    {"path": p.path, "action": p.action, "before_sha256": p.before_sha, "after_sha256": p.after_sha}
                    for p in planned
                ],
                "unchanged": unchanged,
                "entries": entries,
                "catalog_revision": projected.revision,
                "warnings": [i.to_dict() for i in projected.issues if i.severity == "warning"],
            }
            if dry_run or not planned:
                return {**summary, "changed": False, "change": None}
            attr_all = dict(attr)
            attr_all["source_refs"] = [*attr["source_refs"], *batch.source_refs]
            change_id = self._journal(kind, attr_all, planned, [e["ref"] for e in entries], batch.details)
            event = self._apply_and_finalize(change_id)
            return {**summary, "changed": True, "change": event}

    def _record_unobserved_edits(self, paths: set[str]) -> None:
        """Give edits made outside the toolkit to the affected paths their own event first."""
        with self.ctx.store.reader() as conn:
            known = {r["path"]: r["sha256"] for r in conn.execute("SELECT path, sha256 FROM inventory")}
        stale = set()
        for path in paths:
            kind = self.ctx.tree.kind(path)
            current = self.ctx.tree.digest(path) if kind == "file" else None
            if (current.sha256 if current else None) != known.get(path):
                stale.add(path)
        if stale:
            self.changes.scan_locked(dict(EXTERNAL_ATTRIBUTION), paths=stale)

    def _plan(self, ops: list[FileOp]) -> tuple[list[Planned], list[str]]:
        tree = self.ctx.tree
        conflicts: list[dict[str, Any]] = []
        planned: list[Planned] = []
        unchanged: list[str] = []
        batch_paths = {op.path for op in ops}
        for op in ops:
            split_path(op.path)
            parts = op.path.split("/")
            for depth in range(1, len(parts)):
                prefix = "/".join(parts[:depth])
                if prefix in batch_paths or tree.kind(prefix) == "file":
                    conflicts.append({"path": op.path, "reason": "parent_is_file", "parent": prefix})
                    break
            else:
                kind = tree.kind(op.path)
                current = tree.read(op.path) if kind == "file" else None
                current_sha = sha256_hex(current) if current is not None else None
                if kind in ("dir", "other", "symlink"):
                    conflicts.append({"path": op.path, "reason": f"is_{kind}"})
                elif op.action == "create":
                    if current is not None:
                        conflicts.append({"path": op.path, "reason": "already_exists", "current_sha256": current_sha})
                        continue
                    planned.append(Planned(op.path, "create", None, op.content, 0o755 if op.executable else 0o644))
                else:
                    if current is None:
                        conflicts.append({"path": op.path, "reason": "missing", "expected_sha256": op.expected_sha256})
                        continue
                    if current_sha != op.expected_sha256:
                        conflicts.append({"path": op.path, "reason": "hash_mismatch",
                                          "expected_sha256": op.expected_sha256, "current_sha256": current_sha})
                        continue
                    if op.action == "delete":
                        planned.append(Planned(op.path, "delete", current, None, None))
                    else:
                        mode = None
                        if op.executable is not None:
                            mode = 0o755 if op.executable else 0o644
                        current_mode = tree.mode(op.path)
                        if op.content == current and (mode is None or mode == current_mode):
                            unchanged.append(op.path)
                            continue
                        planned.append(Planned(op.path, "edit", current, op.content, mode))
        if conflicts:
            raise ConflictError(
                "the files changed since they were read; nothing was written",
                details={"conflicts": conflicts, "changed": False},
            )
        return planned, unchanged

    # ----------------------------------------------------------------- journal
    def _journal(self, kind: str, attribution: dict[str, Any], planned: list[Planned], entry_refs: list[str],
                 details: dict[str, Any]) -> str:
        ctx = self.ctx
        change_id = new_id("chg")
        with ctx.store.transaction() as conn:
            conn.execute(
                "INSERT INTO pending_changes(change_id, created_at, kind, attribution, package_ids, entry_refs, details)"
                " VALUES (?,?,?,?,?,?,?)",
                (change_id, ctx.now(), kind, dumps(attribution), dumps(package_ids_of([p.path for p in planned])),
                 dumps(sorted(entry_refs)), dumps(details)),
            )
            for ordinal, p in enumerate(planned):
                conn.execute(
                    "INSERT INTO pending_files(change_id, ordinal, path, action, before_sha256, after_sha256,"
                    " before_content, after_content, mode) VALUES (?,?,?,?,?,?,?,?,?)",
                    (change_id, ordinal, p.path, p.action, p.before_sha, p.after_sha, p.before, p.after, p.mode),
                )
        ctx.hook("after_journal", change_id=change_id)
        return change_id

    def _apply_and_finalize(self, change_id: str) -> dict[str, Any]:
        ctx = self.ctx
        with ctx.store.reader() as conn:
            rows = conn.execute("SELECT * FROM pending_files WHERE change_id = ? ORDER BY ordinal", (change_id,)).fetchall()
        try:
            for row in rows:
                self._write_one(row)
                ctx.hook("after_file_write", change_id=change_id, path=row["path"])
            ctx.hook("before_finalize", change_id=change_id)
            return self.finalize(change_id)
        except Exception as exc:
            raise RecoveryRequiredError(
                f"the batch was only partly applied ({type(exc).__name__}: {exc}); run recover",
                details={"pending_change_ids": [change_id], "cause": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def _write_one(self, row: Any) -> None:
        tree = self.ctx.tree
        kind = tree.kind(row["path"])
        current = tree.digest(row["path"]).sha256 if kind == "file" else None
        if kind not in ("file", "missing") or current not in (row["before_sha256"], row["after_sha256"]):
            raise RecoveryConflictError("file changed after the batch was recorded",
                                        details={"path": row["path"], "current_sha256": current})
        if row["action"] == "delete":
            tree.delete(row["path"])
        else:
            tree.write_atomic(row["path"], bytes(row["after_content"]), mode=row["mode"])

    def finalize(self, change_id: str) -> dict[str, Any]:
        """Turn a fully applied pending change into its finalized event (one transaction)."""
        ctx = self.ctx
        with ctx.store.transaction() as conn:
            pending = conn.execute("SELECT * FROM pending_changes WHERE change_id = ?", (change_id,)).fetchone()
            rows = conn.execute("SELECT * FROM pending_files WHERE change_id = ? ORDER BY ordinal", (change_id,)).fetchall()
            for row in rows:
                kind = ctx.tree.kind(row["path"])
                actual = ctx.tree.digest(row["path"]).sha256 if kind == "file" else None
                if kind not in ("file", "missing") or actual != row["after_sha256"]:
                    raise RecoveryConflictError("file changed before its event could be finalized",
                                                details={"path": row["path"], "current_sha256": actual})
            files = [
                {
                    "path": r["path"],
                    "action": r["action"],
                    "before_sha256": r["before_sha256"],
                    "after_sha256": r["after_sha256"],
                    "size": len(r["after_content"]) if r["after_content"] is not None else 0,
                }
                for r in rows
            ]
            insert_event(
                conn,
                change_id=change_id,
                created_at=pending["created_at"],
                kind=pending["kind"],
                attribution=loads(pending["attribution"]),
                files=files,
                entry_refs=loads(pending["entry_refs"]),
                details=loads(pending["details"]),
            )
            conn.execute("DELETE FROM pending_files WHERE change_id = ?", (change_id,))
            conn.execute("DELETE FROM pending_changes WHERE change_id = ?", (change_id,))
            row = conn.execute("SELECT * FROM changes WHERE change_id = ?", (change_id,)).fetchone()
            return event_dict(conn, row)

    # ---------------------------------------------------------------- recovery
    def _pending_state(self) -> list[dict[str, Any]]:
        tree = self.ctx.tree
        with self.ctx.store.reader() as conn:
            pendings = conn.execute("SELECT * FROM pending_changes ORDER BY created_at, change_id").fetchall()
            result = []
            for pending in pendings:
                attribution = loads(pending["attribution"])
                files = []
                for row in conn.execute(
                    "SELECT path, action, before_sha256, after_sha256 FROM pending_files WHERE change_id = ? ORDER BY ordinal",
                    (pending["change_id"],),
                ):
                    try:
                        kind = tree.kind(row["path"])
                        current = tree.digest(row["path"]).sha256 if kind == "file" else None
                    except UnsafePathError:
                        kind, current = "symlink", None
                    if kind in ("dir", "symlink", "other"):
                        state = "conflict"
                    elif current == row["after_sha256"]:
                        state = "applied"
                    elif current == row["before_sha256"]:
                        state = "pending"
                    else:
                        state = "conflict"
                    files.append(
                        {
                            "path": row["path"],
                            "action": row["action"],
                            "state": state,
                            "current_sha256": current,
                            "before_sha256": row["before_sha256"],
                            "after_sha256": row["after_sha256"],
                        }
                    )
                result.append(
                    {
                        "change_id": pending["change_id"],
                        "kind": pending["kind"],
                        "created_at": pending["created_at"],
                        "actor_id": attribution["actor_id"],
                        "files": files,
                    }
                )
        return result

    def recovery_status(self) -> dict[str, Any]:
        with self.ctx.store.lock(exclusive=False):
            pending = self._pending_state()
        return {
            "required": bool(pending),
            "resolvable": all(f["state"] != "conflict" for p in pending for f in p["files"]),
            "pending": pending,
        }

    def recover(self) -> dict[str, Any]:
        """Complete an interrupted batch, or report why it cannot be completed.

        Files are written only when they still match the recorded before
        content; a file matching neither before nor after is never overwritten.
        """
        ctx = self.ctx
        with ctx.store.lock(exclusive=True):
            pending = self._pending_state()
            if not pending:
                return {"recovered": False, "changes": [], "removed_temp_files": ctx.tree.sweep_temp_files()}
            require_editable(f["path"] for p in pending for f in p["files"])
            conflicts = [
                {"change_id": p["change_id"], **f} for p in pending for f in p["files"] if f["state"] == "conflict"
            ]
            if conflicts:
                raise RecoveryConflictError(
                    "an affected file matches neither its recorded before nor after content; nothing was changed",
                    details={"conflicts": conflicts},
                )
            events = []
            for item in pending:
                with ctx.store.reader() as conn:
                    rows = conn.execute(
                        "SELECT * FROM pending_files WHERE change_id = ? ORDER BY ordinal", (item["change_id"],)
                    ).fetchall()
                states = {f["path"]: f["state"] for f in item["files"]}
                for row in rows:
                    if states[row["path"]] == "pending":
                        self._write_one(row)
                events.append(self.finalize(item["change_id"]))
            return {"recovered": True, "changes": events, "removed_temp_files": ctx.tree.sweep_temp_files()}


@dataclass
class BuildContext:
    """What a batch builder may look at, taken under the exclusive lock."""

    base: DiskSource
    catalog: Catalog


def read_snapshot(snapshot: Any) -> dict[str, tuple[bytes, int]]:
    """Read a supplied snapshot into ``{package-relative path: (bytes, mode)}``."""
    if not isinstance(snapshot, dict) or len(snapshot) != 1 or next(iter(snapshot)) not in ("directory", "files"):
        raise InvalidRequestError("snapshot must be {\"directory\": <absolute path>} or {\"files\": [...]}")
    files: dict[str, tuple[bytes, int]] = {}
    total = 0
    if "directory" in snapshot:
        directory = snapshot["directory"]
        if not isinstance(directory, str) or not os.path.isabs(directory):
            raise InvalidRequestError("snapshot.directory must be an absolute path")
        if not os.path.isdir(directory) or os.path.islink(directory):
            raise InvalidRequestError(f"snapshot.directory is not a directory: {directory}")
        # Inspect every directory before SafeTree.walk (which intentionally skips
        # Git metadata). Imports reject it instead of silently discarding it.
        for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
            for name in [*dirnames, *filenames]:
                rel = os.path.relpath(os.path.join(dirpath, name), directory).replace(os.sep, "/")
                split_path(rel, field="snapshot path")
        from .files import SafeTree

        tree = SafeTree(directory)
        for rel, kind in tree.walk():
            if kind != "file":
                raise UnsafePathError(f"snapshot contains a {kind}: {rel}", details={"path": rel})
            data = tree.read(rel, max_bytes=MAX_FILE_BYTES)
            if data is None:
                raise ConflictError("snapshot changed while reading", details={"path": rel})
            total += len(data)
            files[rel] = (data, tree.mode(rel) or 0o644)
            if len(files) > MAX_BATCH_FILES or total > MAX_BATCH_BYTES:
                raise AgenticError("snapshot exceeds authoring limits", code="limit_exceeded")
    else:
        items = snapshot["files"]
        if not isinstance(items, list):
            raise InvalidRequestError("snapshot.files must be a list")
        if len(items) > MAX_BATCH_FILES:
            raise AgenticError(f"snapshot has more than {MAX_BATCH_FILES} files", code="limit_exceeded",
                               details={"limit": MAX_BATCH_FILES})
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise InvalidRequestError(f"snapshot.files[{index}] must be an object")
            unknown = set(item) - {"path", "content", "content_base64", "executable"}
            if unknown:
                raise InvalidRequestError(f"snapshot.files[{index}] has unknown field(s): {sorted(unknown)}")
            split_path(item.get("path"), field=f"snapshot.files[{index}].path")
            if item["path"] in files:
                raise InvalidRequestError(f"snapshot.files repeats path {item['path']!r}")
            if "executable" in item and not isinstance(item["executable"], bool):
                raise InvalidRequestError(f"snapshot.files[{index}].executable must be a boolean")
            data = _decode_content(item, index)
            if len(data) > MAX_FILE_BYTES:
                raise AgenticError(f"snapshot file is larger than {MAX_FILE_BYTES} bytes: {item['path']}",
                                   code="limit_exceeded", details={"path": item["path"], "limit": MAX_FILE_BYTES})
            total += len(data)
            files[item["path"]] = (data, 0o755 if item.get("executable") else 0o644)
    if not files:
        raise ValidationFailedError("the snapshot is empty", details={"issues": ["no files"]})
    if total > MAX_BATCH_BYTES:
        raise AgenticError(f"snapshot content exceeds {MAX_BATCH_BYTES} bytes", code="limit_exceeded",
                           details={"limit": MAX_BATCH_BYTES})
    return files
