"""Stage the owner's viewer for one business's isolated browser; reusable for any explicit business.

Merges one browser surface into ``<instance runtime>/browser-surfaces.json`` (schema 2) and records it as that
business's one browser under ``businesses``. No registered agent is needed or bound. It never edits the database,
contacts Guacamole or starts a unit, and it only ever adds or removes its own surface: other surfaces, every Global
agent's own browser and Desktop binding and other businesses' browsers are preserved. An older registry is refused
untouched; the explicit v1.2 migration converts it. Idempotent; ``dry_run`` writes nothing; the first prior file is
kept for recovery. It runs only when an operator invokes it against explicit instance paths.
"""

import argparse
import fcntl
import hashlib
import json
import os
import sys

from bazarlyk.bootstrap.client import path_has_symlink
from qorqut_operations.contracts import DomainError
from bazarlyk.service import browser_surfaces
from qorqut_operations.storage import safe_io


def _invalid(message):
    return DomainError("invalid_configuration", message)


def active_business(uow, business):
    """(business_id, slug) of the one active business with that slug or UUID."""
    row = uow.connection.execute("SELECT id, slug FROM businesses WHERE (id=? OR slug=?) AND archived_at IS NULL",
                                 (business, business)).fetchone()
    if row is None:
        raise DomainError("not_found", "No active project has that slug or ID")
    return row["id"], row["slug"]


def _empty():
    return {"schema_version": browser_surfaces.SCHEMA_VERSION, "surfaces": {}, "agents": {}, "businesses": {}}


def _load(path):
    """The current document as plain data, or the empty schema-2 document; anything invalid or older is refused."""
    if path_has_symlink(path):
        raise _invalid("The browser surface file cannot follow symlinks")
    try:
        raw = safe_io.read_bytes(path, limit=browser_surfaces.MAX_BYTES, private=True)
    except FileNotFoundError:
        return _empty(), None
    except (DomainError, OSError) as error:
        raise _invalid("The browser surface file must be a private (0600) ordinary file; nothing was changed") from error
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise _invalid("The browser surface file is not valid JSON; nothing was changed") from error
    if not isinstance(value, dict) or value.get("schema_version") != browser_surfaces.SCHEMA_VERSION:
        raise _invalid("The browser surface file is not schema 2; convert it with the explicit v1.2 migration "
                       "before staging; nothing was changed")
    browser_surfaces.parse(value)
    return value, raw


def _surface(surface_id, label, viewer_url, width, height):
    entry = {"label": label, "kind": "browser", "viewer_url": viewer_url}
    if width is not None or height is not None:
        entry.update(width=width, height=height)
    return entry


def _referenced(document, surface_id):
    return (any(surface_id in binding["browsers"] or binding.get("desktop") == surface_id
                for binding in document["agents"].values())
            or any(binding["browser"] == surface_id for binding in document["businesses"].values()))


def merge(document, business_id, *, surface_id, entry, remove=False):
    """The document after making ``surface_id`` the business's one browser (or unbinding it)."""
    result = json.loads(json.dumps(document))
    surfaces, businesses = result["surfaces"], result["businesses"]
    if remove:
        if businesses.get(business_id) == {"browser": surface_id}:
            del businesses[business_id]
        if not _referenced(result, surface_id):
            surfaces.pop(surface_id, None)
        return result
    if surface_id in surfaces and surfaces[surface_id] != entry:
        raise DomainError("configuration_conflict", "That surface ID already names a different viewer; choose a new ID")
    current = businesses.get(business_id)
    if current is not None and current["browser"] != surface_id:
        raise DomainError("configuration_conflict", "The project already has a browser surface; remove it first")
    if any(surface_id in binding["browsers"] or binding.get("desktop") == surface_id for binding in result["agents"].values()):
        raise DomainError("configuration_conflict", "That surface ID is a Global agent's own surface; choose a new ID")
    if any(owner != business_id and binding["browser"] == surface_id for owner, binding in businesses.items()):
        raise DomainError("configuration_conflict", "That surface ID belongs to another project; choose a new ID")
    surfaces[surface_id] = entry
    businesses[business_id] = {"browser": surface_id}
    return result


def _write_private(path, payload):
    if path_has_symlink(path):
        raise _invalid("The browser surface file cannot follow symlinks")
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _encode(document):
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def stage(database, paths, business, *, surface_id, label=None, viewer_url=None, width=None, height=None,
          remove=False, dry_run=False):
    if not browser_surfaces.SURFACE_ID.fullmatch(surface_id or ""):
        raise DomainError("validation_error", "surface_id uses lowercase letters, digits and hyphens and starts with a letter")
    with database.read() as uow:
        business_id, slug = active_business(uow, business)
    path = browser_surfaces.surfaces_path(paths)
    lock = None
    if not dry_run:  # A dry run creates nothing, not even the lock file.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = (path.parent / "browser-surfaces.lock").open("a")
    try:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        document, prior = _load(path)
        entry = None if remove else _surface(surface_id, label or surface_id, viewer_url, width, height)
        updated = merge(document, business_id, surface_id=surface_id, entry=entry, remove=remove)
        browser_surfaces.parse(updated)  # The exact viewer URL, size, ownership and reference rules the reader enforces.
        payload = _encode(updated)
        changed = updated != document  # Content, not formatting: a correct file is never rewritten.
        result = {"business_id": business_id, "slug": slug, "surface_id": surface_id, "removed": remove,
                  "changed": changed, "dry_run": dry_run}
        if changed and not dry_run:
            if prior is not None:
                # Keep the first pre-change file byte for byte so recovery can restore it exactly.
                kept = path.with_name(path.name + ".prior-" + hashlib.sha256(prior).hexdigest()[:12])
                if not kept.exists():
                    _write_private(kept, prior)
            _write_private(path, payload)
        return result
    finally:
        if lock is not None:
            lock.close()


def main(argv=None):
    from bazarlyk.composition.assembly import host_assembly, open_database
    from bazarlyk.service.changes import ChangeFeed
    from bazarlyk.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
    parser = argparse.ArgumentParser(description=__doc__)
    add_instance_arguments(parser)
    parser.add_argument("--business", required=True, help="project slug or UUID")
    parser.add_argument("--surface-id", required=True)
    parser.add_argument("--label")
    parser.add_argument("--viewer-url")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--remove", action="store_true", help="unbind and drop this surface (recovery)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.remove and not args.viewer_url:
            raise DomainError("validation_error", "--viewer-url is required to add a surface")
        paths = instance_paths_from_arguments(args, required=())
        database = open_database(paths, host_assembly(ChangeFeed()))
        result = stage(database, paths, args.business, surface_id=args.surface_id, label=args.label,
                       viewer_url=args.viewer_url, width=args.width, height=args.height,
                       remove=args.remove, dry_run=args.dry_run)
    except DomainError as error:
        print(error.message, file=sys.stderr)  # Fixed domain text only; never the viewer URL.
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
